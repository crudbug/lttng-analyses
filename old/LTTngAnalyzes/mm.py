from LTTngAnalyzes.common import ns_to_hour_nsec


class Mm():
    def __init__(self, mm, cpus, tids, dirty_pages):
        self.mm = mm
        self.cpus = cpus
        self.tids = tids
        self.dirty_pages = dirty_pages
        self.mm["allocated_pages"] = 0
        self.mm["freed_pages"] = 0
        self.mm["count"] = 0
        self.mm["dirty"] = 0
        self.dirty_pages["pages"] = []
        self.dirty_pages["global_nr_dirty"] = -1
        self.dirty_pages["base_nr_dirty"] = -1

    def get_current_proc(self, event):
        cpu_id = event["cpu_id"]
        if cpu_id not in self.cpus:
            return None
        c = self.cpus[cpu_id]
        if c.current_tid == -1:
            return None
        return self.tids[c.current_tid]

    def page_alloc(self, event):
        self.mm["count"] += 1
        self.mm["allocated_pages"] += 1
        for p in self.tids.values():
            if len(p.current_syscall.keys()) == 0:
                continue
            if "alloc" not in p.current_syscall.keys():
                p.current_syscall["alloc"] = 1
            else:
                p.current_syscall["alloc"] += 1
        t = self.get_current_proc(event)
        if t is None:
            return
        t.allocated_pages += 1

    def page_free(self, event):
        self.mm["freed_pages"] += 1
        if self.mm["count"] == 0:
            return
        self.mm["count"] -= 1
        t = self.get_current_proc(event)
        if t is None:
            return
        t.freed_pages += 1

    def block_dirty_buffer(self, event):
        self.mm["dirty"] += 1
        if event["cpu_id"] not in self.cpus.keys():
            return
        c = self.cpus[event["cpu_id"]]
        if c.current_tid <= 0:
            return
        p = self.tids[c.current_tid]
        current_syscall = self.tids[c.current_tid].current_syscall
        if len(current_syscall.keys()) == 0:
            return
        if self.dirty_pages is None:
            return
        if "fd" in current_syscall.keys():
            self.dirty_pages["pages"].append((p, current_syscall["name"],
                                              current_syscall["fd"].filename,
                                              current_syscall["fd"].fd))
        return

    def writeback_global_dirty_state(self, event):
        print("%s count : %d, count dirty : %d, nr_dirty : %d, "
              "nr_writeback : %d, nr_dirtied : %d, nr_written : %d" %
              (ns_to_hour_nsec(event.timestamp), self.mm["count"],
               self.mm["dirty"], event["nr_dirty"],
               event["nr_writeback"], event["nr_dirtied"],
               event["nr_written"]))
        self.mm["dirty"] = 0
