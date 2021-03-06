#!/usr/bin/env python3
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the 'Software'), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

import argparse
import json
import os.path
import socket
import sys
try:
    from babeltrace import TraceCollection
except ImportError:
    # quick fix for debian-based distros
    sys.path.append("/usr/local/lib/python%d.%d/site-packages" %
                    (sys.version_info.major, sys.version_info.minor))
    from babeltrace import TraceCollection
from LTTngAnalyzes.common import ns_to_hour_nsec
from LTTngAnalyzes.state import State
from pymongo import MongoClient
from pymongo.errors import CollectionInvalid

NS_IN_S = 1000000000


class Perf():
    PERF_FORMAT = '{0:18} {1:20} {2:<8} {3:20} {4:<8}'

    def __init__(self, args, traces, types):
        self.args = args

        self.traces = traces
        self.types = types
        self.is_interactive = sys.stdout.isatty()

        self.state = State()
        self.perf = []

        # Stores metadata about processes when outputting to json
        # Keys: PID, values: {pname, threads}
        self.json_metadata = {}
        # Used to identify session in database
        self.session_name = self.args.path.split('/')[-2]
        # Hyphens in collections names are an incovenience in mongo
        self.session_name = self.session_name.replace('-', '_')

    def process_event(self, event):
        if event.name == 'sched_switch':
            ret = self.state.sched.switch(event)
            tid = event['prev_tid']
            if len(ret.keys()) > 0:
                d = {'ts': event.timestamp,
                     'tid': tid}
                for context in ret.keys():
                    if self.types and context not in self.types:
                        continue
                    if context.startswith('perf_'):
                        if self.args.delta:
                            d[context] = ret[context]
                        else:
                            d[context] = self.state.tids[tid].perf[context]
                self.output_perf(event, d)
        elif event.name == 'lttng_statedump_process_state':
            self.state.statedump.process_state(event)

    def run(self):
        '''Process the trace'''
        for event in self.traces.events:
            self.process_event(event)

        if self.args.json:
            self.output_json()

        if self.args.mongo:
            self.store_mongo()

    def output_json(self):
        perf_name = 'perf_' + self.session_name + '.json'
        perf_path = os.path.join(self.args.json, perf_name)
        f = open(perf_path, 'w')
        json.dump(self.perf, f)
        f.close()

        f = open(os.path.join(self.args.json, 'metadata.json'), 'w')
        json.dump(self.json_metadata, f)
        f.close()

    def store_mongo(self):
        client = MongoClient(self.args.mongo_host, self.args.mongo_port)
        db = client.analyses

        perf_name = 'perf_' + self.session_name
        metadata_name = 'metadata_' + self.session_name

        try:
            db.create_collection(perf_name)
        except CollectionInvalid as ex:
            print('Failed to create collection: ')
            print(ex)
            print('Data will not be stored to MongoDB')
            return

        for event in self.perf:
            db[perf_name].insert(event)

        # Ascending timestamp index
        db[perf_name].create_index('ts')

        if metadata_name not in db.collection_names():
            try:
                db.create_collection(metadata_name)
            except CollectionInvalid as ex:
                print('Failed to create collection: ')
                print(ex)
                print('Metadata will not be stored to MongoDB')
                return

            db.sessions.insert({'name': self.session_name})

        for pid in self.json_metadata:
            metadatum = self.json_metadata[pid]
            metadatum['pid'] = pid
            db[metadata_name].update({'pid': pid}, metadatum, upsert=True)

        # Ascending PID index
        db[metadata_name].create_index('pid')

    def output_perf(self, event, ret):
        tid = event['prev_tid']
        if self.args.tid and str(tid) not in self.args.tid:
            return

        pid = self.state.tids[tid].pid
        if pid == -1:
            pid = tid

        comm = self.state.tids[tid].comm
        if self.args.pname is not None and self.args.pname != comm:
            return

        name = event.name
        if name != 'sched_switch':
            return

        endtime = event.timestamp
        if self.args.start and endtime < self.args.start:
            return
        if self.args.end and endtime > self.args.end:
            return

        if not self.args.unixtime:
            endtime = ns_to_hour_nsec(endtime)
        else:
            endtime = '{:.9f}'.format(endtime / NS_IN_S)

        insert = 0
        for context in ret.keys():
            if context.startswith('perf_'):
                if self.args.json or self.args.mongo:
                    insert = 1
                if not self.args.quiet:
                    print(Perf.PERF_FORMAT.format(endtime, comm, tid, context,
                                                  ret[context]))
        if insert:
            self.log_perf_event_json(endtime, comm, tid, pid, ret)

    def log_perf_event_json(self, ts, comm, tid, pid, ret):
        if pid == tid:
            if pid not in self.json_metadata:
                self.json_metadata[pid] = {'pname': comm, 'threads': {}}
            elif self.json_metadata[pid]['pname'] != comm:
                self.json_metadata[pid]['pname'] = comm
        else:
            if pid not in self.json_metadata:
                self.json_metadata[pid] = {'pname': 'unknown', 'threads': {}}

            tid_str = str(tid)
            if tid_str not in self.json_metadata[pid]['threads']:
                self.json_metadata[pid]['threads'][tid_str] = {
                    'pname': comm
                }
            else:
                if self.json_metadata[pid]['threads'][tid_str]['pname'] \
                        != comm:
                    self.json_metadata[pid]['threads'][tid_str]['pname'] = comm

        self.perf.append(ret)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Perf counter analysis')
    parser.add_argument('path', metavar='<path/to/trace>', help='Trace path')
    parser.add_argument('-t', '--type', type=str, default='all',
                        help='Types of perf counters to display')
    parser.add_argument('--tid', type=str, default=0,
                        help='TID for which to display events')
    parser.add_argument('--pname', type=str, default=None,
                        help='Process name for which to display events')
    parser.add_argument('--start', type=int, default=None,
                        help='Start time from which to display events (unix\
                        time)')
    parser.add_argument('--end', type=int, default=None,
                        help='End time after which events are not displayed\
                        (unix time)')
    parser.add_argument('--unixtime', action='store_true',
                        help='Display timestamps in unix time format')
    parser.add_argument('--delta', action='store_true',
                        help='Display deltas instead of total count')
    parser.add_argument('--json', type=str, default=None,
                        help='Store perf counter changes as JSON in specified\
                        directory')
    parser.add_argument('--mongo', type=str, default=None,
                        help='Store perf counter changes into MongoDB at\
                        specified ip and port')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Don\'t output fd events to stdout')

    args = parser.parse_args()

    if args.type != 'all':
        types = args.type.split(',')
    else:
        types = None

    if args.tid:
        args.tid = args.tid.split(',')
    else:
        args.tid = None

    traces = TraceCollection()
    handle = traces.add_traces_recursive(args.path, 'ctf')
    if handle is None:
        sys.exit(1)

    # Convert start/endtime from seconds to nanoseconds
    if args.start:
        args.start = args.start * NS_IN_S
    if args.end:
        args.end = args.end * NS_IN_S

    if args.mongo:
        try:
            (args.mongo_host, args.mongo_port) = args.mongo.split(':')
            socket.inet_aton(args.mongo_host)
            args.mongo_port = int(args.mongo_port)
        except ValueError:
            print('Invalid MongoDB address format: ', args.mongo)
            print('Expected format: IPV4:PORT')
            sys.exit(1)
        except socket.error:
            print('Invalid MongoDB ip ', args.mongo_host)
            sys.exit(1)

    analyser = Perf(args, traces, types)

    analyser.run()

    for h in handle.values():
        traces.remove_trace(h)
