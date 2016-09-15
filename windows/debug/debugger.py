import os.path
from collections import defaultdict, namedtuple
from contextlib import contextmanager

import windows
import windows.winobject.exception as winexception
import windows.native_exec.simple_x86 as x86
import windows.native_exec.simple_x64 as x64

from windows.winobject.process import WinProcess, WinThread
from windows.dbgprint import dbgprint
from windows import winproxy
from windows.generated_def.winstructs import *
from windows.generated_def import windef
from .breakpoints import *

#from windows.syswow64 import CS_32bits
from windows.winobject.exception import VectoredException


PAGE_SIZE = 0x1000


class DEBUG_EVENT(DEBUG_EVENT):
    KNOWN_EVENT_CODE = dict((x,x) for x in [EXCEPTION_DEBUG_EVENT,
        CREATE_THREAD_DEBUG_EVENT, CREATE_PROCESS_DEBUG_EVENT,
        EXIT_THREAD_DEBUG_EVENT, EXIT_PROCESS_DEBUG_EVENT, LOAD_DLL_DEBUG_EVENT,
        UNLOAD_DLL_DEBUG_EVENT, OUTPUT_DEBUG_STRING_EVENT, RIP_EVENT])

    @property
    def code(self):
        return self.KNOWN_EVENT_CODE.get(self.dwDebugEventCode, self.dwDebugEventCode)

WatchedPage = namedtuple('WatchedPage', ["original_prot", "bps"])


class Debugger(object):
    """A debugger based on standard Win32 API. Handle :

        * Standard BP (int3)
        * Hardware-Exec BP (DrX)
        * Memory BP (virtual_protect)"""

    def __init__(self, target):
        """``target`` must be a debuggable :class:`WinProcess`."""
        self._init_dispatch_handlers()
        self.target = target
        self.is_target_launched = False
        #if not already_debuggable:
        #    winproxy.DebugActiveProcess(target.pid)
        self.processes = {}
        self.threads = {}
        self.current_process = None
        self.current_thread = None
        # List of breakpoints
        self.breakpoints = {}
        self._pending_breakpoints = {} #Breakpoints to put in new process / threads
        # Values rewritten by "\xcc"
        self._memory_save = defaultdict(dict)
        # Dict of {tid : {drx taken : BP}}
        self._hardware_breakpoint = {}
        # Breakpoints to reput..
        self._breakpoint_to_reput = {}

        self._module_by_process = {}

        self._pending_breakpoints_new = defaultdict(list)

        self._explicit_single_step = {}

        self._watched_pages = {}# Dict [page_modif] -> [mem bp on the page]

        # [start] -> (size, current_proctection, original_prot)
        self._virtual_protected_memory = [] # List of memory-range modified by a MemBP


    @classmethod
    def attach(cls, target):
        """attach to ``target`` (must be a :class:`WinProcess`)

        :rtype: :class:`Debugger`"""
        winproxy.DebugActiveProcess(target.pid)
        return cls(target)

    def detach(self, target=None):
        """Detach from all debugged processes or process ``target``"""
        if target is None:
            targets = self.processes.values()
            if not targets:
                # We are not following any process
                # maybe a attach/detach with Debugger.loop
                # Just detach from the initial target
                if self.target:
                    tpid = self.target.pid
                    self.target = None  # Remove ref to process -> GC -> CloseHandle -> process is destroyed
                    windows.winproxy.DebugActiveProcessStop(tpid)
                return
            for proc in targets:
                self.detach(proc)
            return
        if not isinstance(target, WinProcess):
            raise ValueError("Detach accept only WinProcess")

        self.disable_all_memory_breakpoints(target)
        for bp in self.breakpoints[target.pid].values():
            if not bp.apply_to_target(target):
                target_threads = [t for t in target.threads if t.tid in self.threads]
                bp_threads = []
                # TODO: clean API tu request HXBP on a thread
                for t in target_threads:
                    t_bps = [pos for pos, hbp in self._hardware_breakpoint[t.tid].items() if hbp == bp]
                    if t_bps:
                       bp_threads.append(t)
                self.del_bp(bp, bp_threads)
            else:
                self.del_bp(bp, [target])

        for thread in [t for t in target.threads if t.tid in self.threads]:
            del self._explicit_single_step[thread.tid]
            del self._breakpoint_to_reput[thread.tid]
            del self.threads[thread.tid]
            ctx = thread.context
            if ctx.EEFlags.TF:  # Remove TRAPFlag before detaching (or it will lead to a crash)
                ctx.EEFlags.TF = 0
                thread.set_context(ctx)
        del self.processes[target.pid]
        del self._watched_pages[target.pid]
        if target is self.current_process:
            self._finish_debug_event(self.REMOVE_ME_debug_event, DBG_CONTINUE)

        if target is self.target:
            self.target = None

        print("Detach from {0}".format(target.pid))
        windows.winproxy.DebugActiveProcessStop(target.pid)

    def _killed_in_action(self):
        """Return ``True`` if current process have been detached by user callback"""
        # Fix ? _handle_exit_process remove from processes but need a FinishDebugEvent
        return self.current_process.pid not in self.processes


    @classmethod
    def debug(cls, path, args=None, dwCreationFlags=0, show_windows=False):
        """Create a process and debug it.

        :rtype: :class:`Debugger`"""
        dwCreationFlags |= DEBUG_PROCESS
        c = windows.utils.create_process(path, args=args, dwCreationFlags=dwCreationFlags, show_windows=show_windows)
        return cls(c)

    def _init_dispatch_handlers(self):
        dbg_evt_dispatch = {}
        dbg_evt_dispatch[EXCEPTION_DEBUG_EVENT] = self._handle_exception
        dbg_evt_dispatch[CREATE_THREAD_DEBUG_EVENT] = self._handle_create_thread
        dbg_evt_dispatch[CREATE_PROCESS_DEBUG_EVENT] = self._handle_create_process
        dbg_evt_dispatch[EXIT_PROCESS_DEBUG_EVENT] = self._handle_exit_process
        dbg_evt_dispatch[EXIT_THREAD_DEBUG_EVENT] = self._handle_exit_thread
        dbg_evt_dispatch[LOAD_DLL_DEBUG_EVENT] = self._handle_load_dll
        dbg_evt_dispatch[UNLOAD_DLL_DEBUG_EVENT] = self._handle_unload_dll
        dbg_evt_dispatch[RIP_EVENT] = self._handle_rip
        dbg_evt_dispatch[OUTPUT_DEBUG_STRING_EVENT] = self._handle_output_debug_string
        self._DebugEventCode_dispatch = dbg_evt_dispatch

    def _debug_event_generator(self):
        while True:
            debug_event = DEBUG_EVENT()
            winproxy.WaitForDebugEvent(debug_event)
            yield debug_event

    def _finish_debug_event(self, event, action):
        if action not in [windef.DBG_CONTINUE, windef.DBG_EXCEPTION_NOT_HANDLED]:
            raise ValueError('Unknow action : <0>'.format(action))
        winproxy.ContinueDebugEvent(event.dwProcessId, event.dwThreadId, action)

    def _update_debugger_state(self, debug_event):
        self.current_process = self.processes[debug_event.dwProcessId]
        self.current_thread = self.threads[debug_event.dwThreadId]

    def _dispatch_debug_event(self, debug_event):
        #print("DISPATCH {0}".format(DEBUG_EVENT.KNOWN_EVENT_CODE.get(debug_event.dwDebugEventCode)))
        handler = self._DebugEventCode_dispatch.get(debug_event.dwDebugEventCode, self._handle_unknown_debug_event)
        return handler(debug_event)

    def _dispatch_breakpoint(self, exception, addr):
        bp = self.breakpoints[self.current_process.pid][addr]
        with self.DisabledMemoryBreakpoint():
            x = bp.trigger(self, exception)
        return x

    def _resolve(self, addr, target):
        if not isinstance(addr, basestring):
            return addr
        dll, api = addr.split("!")
        dll = dll.lower()
        modules = self._module_by_process[target.pid]
        mod = None
        if dll in modules:
            mod = [modules[dll]]
        if not mod:
            return None
        # TODO: optim exports are the same for whole system (32 vs 64 bits)
        # I don't have to reparse the exports each time..
        # Try to interpret api as an int
        try:
            api_int = int(api, 0)
            return mod[0].baseaddr + api_int
        except ValueError:
            pass
        exports = mod[0].exports
        if api not in exports:
            raise ValueError("Unknown API <{0}> in DLL {1}".format(api, dll))
        return exports[api]

    def add_pending_breakpoint(self, bp, target):
        self._pending_breakpoints_new[target].append(bp)

    def remove_pending_breakpoint(self, bp, target):
        self._pending_breakpoints_new[target].remove(bp)

    def _setup_breakpoint(self, bp, target):
        _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
        if target is None:
            if bp.type in [STANDARD_BP, MEMORY_BREAKPOINT]: #TODO: better..
                targets = self.processes.values()
            else:
                targets = self.threads.values()
        else:
            targets = [target]
        for target in targets:
            return _setup_method(bp, target)

    def _restore_breakpoints(self):
        for bp in self._breakpoint_to_reput[self.current_thread.tid]:
            if bp.type == HARDWARE_EXEC_BP:
                raise NotImplementedError("Why is this here ? we use RF flags to pass HXBP")
            restore = getattr(self, "_restore_breakpoint_" + bp.type)
            restore(bp, self.current_process)
        del self._breakpoint_to_reput[self.current_thread.tid][:]
        return

    def _setup_breakpoint_BP(self, bp, target):
        if not isinstance(target, WinProcess):
            raise ValueError("SETUP STANDARD_BP on {0}".format(target))

        addr = self._resolve(bp.addr, target)
        if addr is None:
            return False
        bp._addr = addr
        self._memory_save[target.pid][addr] = target.read_memory(addr, 1)
        self.breakpoints[target.pid][addr] = bp
        target.write_memory(addr, "\xcc")
        return True

    def _restore_breakpoint_BP(self, bp, target):
        self._memory_save[target.pid][bp._addr] = target.read_memory(bp._addr, 1)
        return target.write_memory(bp._addr, "\xcc")

    def _remove_breakpoint_BP(self, bp, target):
        if not isinstance(target, WinProcess):
            raise ValueError("SETUP STANDARD_BP on {0}".format(target))
        addr = self._resolve(bp.addr, target)
        target.write_memory(addr, self._memory_save[target.pid][addr])
        del self._memory_save[target.pid][addr]
        del self.breakpoints[target.pid][addr]
        return True

    def _setup_breakpoint_HXBP(self, bp, target):
        #print("Setup {0} into {1}".format(bp, target))
        if not isinstance(target, WinThread):
            raise ValueError("SETUP HXBP_BP on {0}".format(target))
        # Todo: opti, not reparse exports for all thread of the same process..
        addr = self._resolve(bp.addr, target.owner)
        if addr is None:
            return False
        x = self._hardware_breakpoint[target.tid]
        if all(pos in x for pos in range(4)):
            raise ValueError("Cannot put {0} in {1} (DRx full)".format(bp, target))
        empty_drx = str([pos for pos in range(4) if pos not in x][0])
        ctx = target.context
        ctx.EDr7.GE = 1
        ctx.EDr7.LE = 1
        setattr(ctx.EDr7, "L" + empty_drx, 1)
        setattr(ctx, "Dr" + empty_drx, addr)
        x[int(empty_drx)] = bp
        target.set_context(ctx)
        self.breakpoints[target.owner.pid][addr] = bp
        return True

    def _remove_breakpoint_HXBP(self, bp, target):
        if not isinstance(target, WinThread):
            raise ValueError("SETUP HXBP_BP on {0}".format(target))
        addr = self._resolve(bp.addr, target.owner)
        bp_pos = [pos for pos, hbp in self._hardware_breakpoint[target.tid].items() if hbp == bp]
        if not bp_pos:
            raise ValueError("Asked to remove {0} from {1} but not present in hbp_list".format(bp, target))
        bp_pos_str = str(bp_pos[0])
        ctx = target.context
        setattr(ctx.EDr7, "L" + bp_pos_str, 0)
        setattr(ctx, "Dr" + bp_pos_str, 0)
        target.set_context(ctx)
        try: # TODO: vraiment faire les HXBP par thread ? ...
            del self.breakpoints[target.owner.pid][addr]
        except:
            pass
        return True

    ## MemBP internal helpers
    def _compute_page_access_for_event(self, target, events):
        if "R" in events:
            return PAGE_NOACCESS
        if set("WX").issubset(events):
            return PAGE_READONLY
        if events == set("W"):
            return PAGE_EXECUTE_READ
        if events == set("X"):
            # Might have problem if DEP is not enabled
            if windows.winproxy.is_implemented(windows.winproxy.GetProcessDEPPolicy):
                has_DEP = DWORD()
                permaned = LONG()
                windows.winproxy.GetProcessDEPPolicy(target.handle, has_DEP, permaned)
                has_DEP = has_DEP.value
            else:
                has_DEP = 0
            return PAGE_READWRITE if has_DEP else PAGE_NOACCESS
        raise ValueError("Unexpected set of event for Membp: {0}".format(events))


    def _setup_breakpoint_MEMBP(self, bp, target):
        addr = self._resolve(bp.addr, target)
        bp._addr = addr
        self._events = set(bp.events)
        if addr is None:
            return False
        # Split in affected pages:
        protection_for_bp = self._compute_page_access_for_event(target, self._events)
        affected_pages = range((addr >> 12) << 12, addr + bp.size, PAGE_SIZE)
        old_prot = DWORD()
        cp_watch_page = self._watched_pages[self.current_process.pid]
        for page_addr in affected_pages:
            if page_addr not in cp_watch_page:
                target.virtual_protect(page_addr, PAGE_SIZE, protection_for_bp, old_prot)
                # Page with no other MemBP
                cp_watch_page[page_addr] = WatchedPage(old_prot.value, [bp])
            else:
                # Reduce the right of the page to the common need
                cp_watch_page[page_addr].bps.append(bp)
                full_page_events = set.union(*[bp.events for bp in cp_watch_page[page_addr].bps])
                protection_for_page = self._compute_page_access_for_event(target, full_page_events)
                target.virtual_protect(page_addr, PAGE_SIZE, protection_for_page, None)
                # TODO: watch for overlap with other MEM breakpoints
        return True

    def _restore_breakpoint_MEMBP(self, bp, target):
        (page_addr, page_prot) = bp._reput_page
        return target.virtual_protect(page_addr, PAGE_SIZE, page_prot, None)


    def _remove_breakpoint_MEMBP(self, bp, target):
        affected_pages = range((bp._addr >> 12) << 12, bp._addr + bp.size, PAGE_SIZE)
        vprot_begin = affected_pages[0]
        vprot_size = PAGE_SIZE * len(affected_pages)
        cp_watch_page = self._watched_pages[self.current_process.pid]
        for page_addr in affected_pages:
            cp_watch_page[page_addr].bps.remove(bp)
            if not cp_watch_page[page_addr].bps:
                target.virtual_protect(page_addr, PAGE_SIZE, cp_watch_page[page_addr].original_prot, None)
                del cp_watch_page[page_addr]
            else:
                full_page_events = set.union(*[bp.events for bp in cp_watch_page[page_addr].bps])
                protection_for_page = self._compute_page_access_for_event(target, full_page_events)
                target.virtual_protect(page_addr, PAGE_SIZE, protection_for_page, None)
        return True


    def _setup_pending_breakpoints_new_process(self, new_process):
        for bp in self._pending_breakpoints_new[None]:
            if bp.apply_to_target(new_process): #BP for thread or process ?
                _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
                _setup_method(bp, new_process)

        for bp in list(self._pending_breakpoints_new[new_process.pid]):
            if  bp.apply_to_target(new_process):
                _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
                if _setup_method(bp, new_process):
                    self._pending_breakpoints_new[new_process.pid].remove(bp)

    def _setup_pending_breakpoints_new_thread(self, new_thread):
        for bp in self._pending_breakpoints_new[None]:
            if bp.apply_to_target(new_thread): #BP for thread or process ?
                _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
                _setup_method(bp, new_thread)

        for bp in self._pending_breakpoints_new[new_thread.owner.pid]:
            if bp.apply_to_target(new_thread):
                _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
                _setup_method(bp, new_thread)

        for bp in list(self._pending_breakpoints_new[new_thread.tid]):
            _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
            if _setup_method(bp, new_thread):
                self._pending_breakpoints_new[new_thread.tid].remove(bp)


    def _setup_pending_breakpoints_load_dll(self, dll_name):
        for bp in self._pending_breakpoints_new[None]:
            if isinstance(bp.addr, basestring):
                target_dll = bp.addr.lower().split("!")[0]
                if target_dll == dll_name:
                    _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
                    if bp.apply_to_target(self.current_process):
                        _setup_method(bp, self.current_process)
                    else:
                        for t in [t for t in self.current_process.threads if t.tid in self.threads]:
                            _setup_method(bp, t)

        for bp in self._pending_breakpoints_new[self.current_process.pid]:
            if isinstance(bp.addr, basestring):
                target_dll = bp.addr.split("!")[0]
                if target_dll == dll_name:
                    _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
                    _setup_method(bp, self.current_process)

        for thread in self.current_process.threads:
            for bp in self._pending_breakpoints_new[thread.tid]:
                if isinstance(bp.addr, basestring):
                    target_dll = bp.addr.split("!")[0]
                    if target_dll == dll_name:
                        _setup_method = getattr(self, "_setup_breakpoint_" + bp.type)
                        _setup_method(bp, self.thread)

    def _pass_breakpoint(self, addr):
        process = self.current_process
        thread = self.current_thread
        process.write_memory(addr, self._memory_save[process.pid][addr])
        regs = thread.context
        regs.EFlags |= (1 << 8)
        #regs.pc -= 1 # Done in _handle_exception_breakpoint before dispatch
        thread.set_context(regs)
        bp = self.breakpoints[self.current_process.pid][addr]
        self._breakpoint_to_reput[thread.tid].append(bp) #Register pending breakpoint for next single step

    def _pass_memory_breakpoint(self, bp, page_protect, fault_page):
        cp = self.current_process
        page_prot = DWORD()
        cp.virtual_protect(fault_page, PAGE_SIZE, page_protect, page_prot)
        thread = self.current_thread
        ctx = thread.context
        ctx.EEFlags.TF = 1
        thread.set_context(ctx)
        bp._reput_page = (fault_page, page_prot.value)
        self._breakpoint_to_reput[thread.tid].append(bp)

    # debug event handlers
    def _handle_unknown_debug_event(self, debug_event):
        raise NotImplementedError("dwDebugEventCode = {0}".format(debug_event.dwDebugEventCode))


    def _handle_exception_breakpoint(self, exception, excp_addr):
        excp_bitness = self.get_exception_bitness(exception)
        if excp_addr in self.breakpoints[self.current_process.pid]:
            thread = self.current_thread
            if self.current_process.bitness == 32 and excp_bitness == 64:
                ctx = thread.context_syswow
            else:
                ctx = thread.context
            ctx.pc -= 1
            if self.current_process.bitness == 32 and excp_bitness == 64:
                thread.set_syswow_context(ctx)
            else:
                thread.set_context(ctx)
            continue_flag = self._dispatch_breakpoint(exception, excp_addr)
            if self._killed_in_action():
                return continue_flag
            self._explicit_single_step[self.current_thread.tid] = self.current_thread.context.EEFlags.TF
            if excp_addr in self.breakpoints[self.current_process.pid]:
                # Setup BP if not suppressed
                self._pass_breakpoint(excp_addr)
            return continue_flag
        with self.DisabledMemoryBreakpoint():
            return self.on_exception(exception)

    def _handle_exception_singlestep(self, exception, excp_addr):
        if self.current_thread.tid in self._breakpoint_to_reput and self._breakpoint_to_reput[self.current_thread.tid]:
            self._restore_breakpoints()
            if self._explicit_single_step[self.current_thread.tid]:
                with self.DisabledMemoryBreakpoint():
                    self.on_single_step(exception)
            if not self._killed_in_action():
                self._explicit_single_step[self.current_thread.tid] = self.current_thread.context.EEFlags.TF
            return DBG_CONTINUE
        elif excp_addr in self.breakpoints[self.current_process.pid]:
            # Verif that's not a standard BP ?
            bp = self.breakpoints[self.current_process.pid][excp_addr]
            with self.DisabledMemoryBreakpoint():
                bp.trigger(self, exception)
            if self._killed_in_action():
                return DBG_CONTINUE
            ctx = self.current_thread.context
            self._explicit_single_step[self.current_thread.tid] = ctx.EEFlags.TF
            if excp_addr in self.breakpoints[self.current_process.pid]:
                ctx.EEFlags.RF = 1
                self.current_thread.set_context(ctx)
            return DBG_CONTINUE
        elif self._explicit_single_step[self.current_thread.tid]:
            with self.DisabledMemoryBreakpoint():
                continue_flag = self.on_single_step(exception)
            if self._killed_in_action():
                return continue_flag
            self._explicit_single_step[self.current_thread.tid] = self.current_thread.context.EEFlags.TF
            return continue_flag
        else:
            with self.DisabledMemoryBreakpoint():
                continue_flag = self.on_exception(exception)
            if self._killed_in_action():
                return continue_flag
            self._explicit_single_step[self.current_thread.tid] = self.current_thread.context.EEFlags.TF
            return continue_flag

    #  === Testing PAGE_NOACCESS(0x1L) ===
    #  exception: access violation reading 0x00470000
    #  exception: access violation writing 0x00470000
    #  === Testing PAGE_READONLY(0x2L) ===
    #  exception: access violation writing 0x00470000
    #  === Testing PAGE_READWRITE(0x4L) ===
    #  === Testing PAGE_EXECUTE(0x10L) ===
    #  exception: access violation writing 0x00470000
    #  === Testing PAGE_EXECUTE_READ(0x20L) ===
    #  exception: access violation writing 0x00470000
    #  === Testing PAGE_EXECUTE_READWRITE(0x40L) ===

    def _handle_exception_access_violation(self, exception, excp_addr):
        READ = 0
        WRITE = 1
        EXEC = 2
        EVENT_STR = "RWX"

        fault_type = exception.ExceptionRecord.ExceptionInformation[0]
        fault_addr = exception.ExceptionRecord.ExceptionInformation[1]
        pc_addr = self.current_thread.context.pc
        if fault_addr == pc_addr:
            fault_type = EXEC
        event = EVENT_STR[fault_type]

        fault_page = (fault_addr >> 12) << 12
        cp_watch_page = self._watched_pages[self.current_process.pid]

        mem_bp = self.get_memory_breakpoint_at(fault_addr, self.current_process)
        if mem_bp is False: # No BP on this page
            with self.DisabledMemoryBreakpoint():
                return self.on_exception(exception)
        original_prot = cp_watch_page[fault_page].original_prot
        if mem_bp is None or event not in mem_bp.events: # Page has MEMBP but None handle this address | event not asked by membp
            # This hack is bad, find a BP on the page to restore original access..
            bp = cp_watch_page[fault_page].bps[-1]
            self._pass_memory_breakpoint(bp, original_prot, fault_page)
            return DBG_CONTINUE

        with self.DisabledMemoryBreakpoint():
            continue_flag = mem_bp.trigger(self, exception)
        if self._killed_in_action():
            return continue_flag
        self._explicit_single_step[self.current_thread.tid] = self.current_thread.context.EEFlags.TF
        # If BP has not been removed in trigger, pas it
        if fault_page in cp_watch_page and mem_bp in cp_watch_page[fault_page].bps:
            self._pass_memory_breakpoint(mem_bp, original_prot, fault_page)
        return continue_flag


    # TODO: self._explicit_single_step setup by single_step() ? check at the end ? finally ?
    def _handle_exception(self, debug_event):
        """Handle EXCEPTION_DEBUG_EVENT"""
        exception = debug_event.u.Exception
        self._update_debugger_state(debug_event)

        if windows.current_process.bitness == 32:
            exception.__class__ = winexception.EEXCEPTION_DEBUG_INFO32
        else:
            exception.__class__ = winexception.EEXCEPTION_DEBUG_INFO64

        excp_code = exception.ExceptionRecord.ExceptionCode
        excp_addr = exception.ExceptionRecord.ExceptionAddress
        if excp_code in [EXCEPTION_BREAKPOINT, STATUS_WX86_BREAKPOINT] and excp_addr in self.breakpoints[self.current_process.pid]:
            return self._handle_exception_breakpoint(exception, excp_addr)
        elif excp_code in [EXCEPTION_SINGLE_STEP, STATUS_WX86_SINGLE_STEP]:
            return self._handle_exception_singlestep(exception, excp_addr)
        elif excp_code == EXCEPTION_ACCESS_VIOLATION:
            return self._handle_exception_access_violation(exception, excp_addr)
        else:
            with self.DisabledMemoryBreakpoint():
                continue_flag = self.on_exception(exception)
            if self._killed_in_action():
                return continue_flag
            self._explicit_single_step[self.current_thread.tid] = self.current_thread.context.EEFlags.TF
            return continue_flag


    def _get_loaded_dll(self, load_dll):
        name_sufix = ""
        pe = windows.pe_parse.GetPEFile(load_dll.lpBaseOfDll, self.current_process)
        if self.current_process.bitness == 32 and pe.bitness == 64:
            name_sufix = "64"

        if not load_dll.lpImageName:
            return pe.export_name + name_sufix
        try:
            addr = self.current_process.read_ptr(load_dll.lpImageName)
        except:
            addr = None

        if not addr:
            pe = windows.pe_parse.GetPEFile(load_dll.lpBaseOfDll, self.current_process)
            dll_name = pe.export_name
            if not dll_name:
                dll_name = os.path.basename(self.current_process.get_mapped_filename(load_dll.lpBaseOfDll))
            return dll_name + name_sufix

        if load_dll.fUnicode:
            return self.current_process.read_wstring(addr) + name_sufix
        return self.current_process.read_string(addr) + name_sufix

    def _handle_create_process(self, debug_event):
        """Handle CREATE_PROCESS_DEBUG_EVENT"""
        create_process = debug_event.u.CreateProcessInfo
        # Duplicate handle, so garbage collection of the process/thread does not
        # break the debug API invariant (those x_event handle are close by the debug API  itself)
        proc_handle = HANDLE()
        thread_handle = HANDLE()
        cp_handle = windows.current_process.handle

        winproxy.DuplicateHandle(cp_handle, create_process.hProcess, cp_handle, ctypes.byref(proc_handle), dwOptions=DUPLICATE_SAME_ACCESS)
        winproxy.DuplicateHandle(cp_handle, create_process.hThread, cp_handle, ctypes.byref(thread_handle), dwOptions=DUPLICATE_SAME_ACCESS)

        dbgprint(" Got PROC handle {0:#x}".format(create_process.hProcess, self), "HANDLE")
        dbgprint(" PROC handle duplicated: {0:#x}".format(proc_handle.value), "HANDLE")

        dbgprint(" Got THREAD handle {0:#x}".format(create_process.hThread, self), "HANDLE")
        dbgprint(" THREAD handle duplicated: {0:#x}".format(thread_handle.value), "HANDLE")

        self.current_process = WinProcess._from_handle(proc_handle.value)
        self.current_thread = WinThread._from_handle(thread_handle.value)

        self.threads[self.current_thread.tid] = self.current_thread
        self._explicit_single_step[self.current_thread.tid] = False
        self._hardware_breakpoint[self.current_thread.tid] = {}
        self._breakpoint_to_reput[self.current_thread.tid] = []
        self.processes[self.current_process.pid] = self.current_process
        self._watched_pages[self.current_process.pid] = {} #defaultdict(list)
        self.breakpoints[self.current_process.pid] = {}
        self._module_by_process[self.current_process.pid] = {}
        self._update_debugger_state(debug_event)
        self._setup_pending_breakpoints_new_process(self.current_process)
        self._setup_pending_breakpoints_new_thread(self.current_thread)
        with self.DisabledMemoryBreakpoint():
            return self.on_create_process(create_process)
        # TODO: close hFile

    def _handle_exit_process(self, debug_event):
        """Handle EXIT_PROCESS_DEBUG_EVENT"""
        self._update_debugger_state(debug_event)
        print("Exit process !!!")
        #import pdb;pdb.set_trace()
        exit_process = debug_event.u.ExitProcess
        retvalue = self.on_exit_process(exit_process)
        del self.threads[self.current_thread.tid]
        del self._explicit_single_step[self.current_thread.tid]
        del self._hardware_breakpoint[self.current_thread.tid]
        del self._breakpoint_to_reput[self.current_thread.tid]
        del self.processes[self.current_process.pid]
        del self._watched_pages[self.current_process.pid]

        del self._module_by_process[self.current_process.pid]

        # GC EXPLORATION CODE
        import gc
        over = gc.get_referrers
        under = gc.get_referents
        ####

        cpid = self.current_process.pid
        del self.current_thread
        ## This should trigger DEL of the self.current_process
        #print("self.current_process WILL BE DELETED")
        del self.current_process
        #print("self.current_process DELETED")

        if cpid == self.target.pid:
            #del self.target
            print("DEL TARGET")
            del self.target
            #import pdb;pdb.set_trace()

        # This is like.. the WORST PATCH EVER
        # I'am going to sleep so here the problem for when it will time to fix this:
        # The PEFile class is a mess: too much cell arround target
        # This means that destroying them is not enought to __del__ the target (dbg.current_process)
        # So the dbg.current_process is still alive, so handle is also still alive..
        # For now we need to force gc.collect
        # This will need a rewrite of GetPEFile...
        import gc; gc.collect()
        return retvalue

    def _handle_create_thread(self, debug_event):
        """Handle CREATE_THREAD_DEBUG_EVENT"""
        create_thread = debug_event.u.CreateThread
        # Duplicate handle, so garbage collection of the thread does not
        # break the debug API invariant (those x_event handle are close by the debug API  itself)
        thread_handle = HANDLE()
        cp_handle = windows.current_process.handle
        winproxy.DuplicateHandle(cp_handle, create_thread.hThread, cp_handle, ctypes.byref(thread_handle), dwOptions=DUPLICATE_SAME_ACCESS)
        self.current_thread = WinThread._from_handle(thread_handle.value)
        self.threads[self.current_thread.tid] = self.current_thread
        self._explicit_single_step[self.current_thread.tid] = False
        self._breakpoint_to_reput[self.current_thread.tid] = []
        self._hardware_breakpoint[self.current_thread.tid] = {}
        self._setup_pending_breakpoints_new_thread(self.current_thread)
        with self.DisabledMemoryBreakpoint():
            return self.on_create_thread(create_thread)


    def _handle_exit_thread(self, debug_event):
        """Handle EXIT_THREAD_DEBUG_EVENT"""
        self._update_debugger_state(debug_event)
        exit_thread = debug_event.u.ExitThread
        with self.DisabledMemoryBreakpoint():
            retvalue = self.on_exit_thread(exit_thread)
        del self.threads[self.current_thread.tid]
        del self._hardware_breakpoint[self.current_thread.tid]
        del self._explicit_single_step[self.current_thread.tid]
        del self._breakpoint_to_reput[self.current_thread.tid]
        return retvalue

    def _handle_load_dll(self, debug_event):
        """Handle LOAD_DLL_DEBUG_EVENT"""
        self._update_debugger_state(debug_event)
        load_dll = debug_event.u.LoadDll
        dll = self._get_loaded_dll(load_dll)
        dll_name = os.path.basename(dll).lower()
        if dll_name.endswith(".dll"):
            dll_name = dll_name[:-4]
        if dll_name.endswith(".dll64"):
            dll_name = dll_name[:-6] +  "64" # Crade..
        #print("Load {0} -> {1}".format(dll, dll_name))
        self._module_by_process[self.current_process.pid][dll_name] = windows.pe_parse.GetPEFile(load_dll.lpBaseOfDll, self.current_process)
        self._setup_pending_breakpoints_load_dll(dll_name)
        with self.DisabledMemoryBreakpoint():
            return self.on_load_dll(load_dll)

    def _handle_unload_dll(self, debug_event):
        """Handle UNLOAD_DLL_DEBUG_EVENT"""
        self._update_debugger_state(debug_event)
        unload_dll = debug_event.u.UnloadDll
        with self.DisabledMemoryBreakpoint():
            return self.on_unload_dll(unload_dll)

    def _handle_output_debug_string(self, debug_event):
        """Handle OUTPUT_DEBUG_STRING_EVENT"""
        self._update_debugger_state(debug_event)
        debug_string = debug_event.u.DebugString
        with self.DisabledMemoryBreakpoint():
            return self.on_output_debug_string(debug_string)

    def _handle_rip(self, debug_event):
        """Handle RIP_EVENT"""
        self._update_debugger_state(debug_event)
        rip_info = debug_event.u.RipInfo
        with self.DisabledMemoryBreakpoint():
            return self.on_rip(rip_info)

    ## Public API
    def loop(self):
        """Debugging loop: handle event / dispatch to breakpoint. Returns when all targets are dead/detached"""
        for debug_event in self._debug_event_generator():
            self.REMOVE_ME_debug_event = debug_event
            dbg_continue_flag = self._dispatch_debug_event(debug_event)
            if dbg_continue_flag is None:
                dbg_continue_flag = DBG_CONTINUE
            if debug_event.dwDebugEventCode == EXIT_PROCESS_DEBUG_EVENT or not self._killed_in_action():
            #if not self._killed_in_action():
                # should we always _finish_debug_event even if process was killed ?
                # rhaaa _killed_in_action is a REALLY bad name, it's not killed, it's detached
                # TODO: FIXME
                self._finish_debug_event(debug_event, dbg_continue_flag)
            if not self.processes:
                break

    def add_bp(self, bp, addr=None, type=None, target=None):
        """Add a breakpoint, bp can be:

            * a :class:`Breakpoint` (addr and type must be ``None``)
            * any callable (addr and type must NOT be ``None``) (NON-TESTED)

            If the ``bp`` type is ``STANDARD_BP`` or ``MEMORY_BREAKPOINT``, target can be ``None`` (all targets) or a process.

            If the ``bp`` type is ``HARDWARE_EXEC_BP``, target can be ``None`` (all targets), a process or a thread.
        """
        if getattr(bp, "addr", None) is None:
            if addr is None or type is None:
                raise ValueError("SUCK YOUR NONE")
            bp = ProxyBreakpoint(bp, addr, type)
        else:
            if addr is not None or type is not None:
                raise ValueError("Given <addr|type> by parameters but BP object have them")
        del addr
        del type

        if target is None:
            # Need to add it to all other breakpoint
            self.add_pending_breakpoint(bp, None)
        elif target is not None:
            # Check that targets are accepted
            if target not in self.processes.values() + self.threads.values():
                if target == self.target: # Original target (that have not been lauched yet)
                    return self.add_pending_breakpoint(bp, target)
                else:
                    raise ValueError("Unknown target {0}".format(target))
        return self._setup_breakpoint(bp, target)

    def del_bp(self, bp, targets=None):
        """Delete a breakpoint, if targets is ``None``: delete it from all targets"""
        original_target = targets
        _remove_method = getattr(self, "_remove_breakpoint_" + bp.type)
        if targets is None:
            if bp.type in [STANDARD_BP, MEMORY_BREAKPOINT]: #TODO: better..
                targets = self.processes.values()
            else:
                targets = self.threads.values()
        for target in targets:
            _remove_method(bp, target)
        if original_target is None:
            return self.remove_pending_breakpoint(bp, original_target)

    def single_step(self):
        """Make the ``current_thread`` ``single_step``. ``Debugger.on_single_step`` will be called after that"""
        t = self.current_thread
        ctx = t.context
        ctx.EEFlags.TF = 1
        t.set_context(ctx)

    ## Memory Breakpoint helper
    def get_memory_breakpoint_at(self, addr, process=None):
        """Get the memory breakpoint that handle ``addr``

        Return values are:

            * ``False`` if the page has no memory breakpoint (real fault)
            * ``None`` if the page as memBP but None handle ``addr``
            * ``bp`` the MemBP that handle ``addr``
        """
        if process is None:
            process = self.current_process

        fault_page = (addr >> 12) << 12
        if fault_page not in self._watched_pages[process.pid]:
            return False

        for bp in self._watched_pages[process.pid][fault_page].bps:
            if bp._addr <= addr < bp._addr + bp.size:
                return bp
        return None

    def disable_all_memory_breakpoints(self, target=None):
        """Restore all pages to their original access rights.
           If target is ``None``, use ``current_process``

           :return: a mapping of all disabled breakpoints that must be passed to :func:`restore_all_memory_breakpoints`"""
        if target is None:
            target = self.current_process
        res = {}
        cp_watch_page = self._watched_pages[target.pid]
        page_protection = DWORD()
        for page_addr, watched_page in cp_watch_page.items():
            target.virtual_protect(page_addr, PAGE_SIZE, watched_page.original_prot, page_protection)
            res[page_addr] = page_protection.value
        return res


    def restore_all_memory_breakpoints(self, data, target=None):
        """Re-setup all memory breakpoints, affecting pages access rights.
           If target is ``None``, use ``current_process``

           ``data`` is the result of the corresponding call to :func:`disable_all_memory_breakpoints`"""
        if target is None:
            target = self.current_process
        for page_addr, protection in data.items():
            # Prevent restoring deleted breakpoints
            if page_addr in self._watched_pages[target.pid]:
                target.virtual_protect(page_addr, PAGE_SIZE, protection, None)
        return

    @contextmanager
    def DisabledMemoryBreakpoint(self, target=None):
        """A context-manager that disable all memory breakpoints and restore them on exit"""
        data = self.disable_all_memory_breakpoints(target)
        try:
            yield
        finally:
            if not self._killed_in_action():
                self.restore_all_memory_breakpoints(data, target)

    def get_exception_bitness(self, exc):
        """Return the bitness in which the exception occured.
           Useful when debugingg a 32b process from a 64bits one

           :return: :class:`int` -- 32 or 64"""
        if windows.current_process.bitness == 32:
            return 32
        if exc.ExceptionRecord.ExceptionCode in [STATUS_WX86_BREAKPOINT, STATUS_WX86_SINGLE_STEP]:
            return 32
        return 64

    # Public callback
    def on_exception(self, exception):
        """Called on exception event other that known breakpoint or requested single step. ``exception`` is one of the following type:

                * :class:`windows.winobject.exception.EEXCEPTION_DEBUG_INFO32`
                * :class:`windows.winobject.exception.EEXCEPTION_DEBUG_INFO64`

           The default behaviour is to return ``DBG_CONTINUE`` for the known exception code
           and ``DBG_EXCEPTION_NOT_HANDLED`` else
        """
        if not exception.ExceptionRecord.ExceptionCode in winexception.exception_name_by_value:
            return DBG_EXCEPTION_NOT_HANDLED
        return DBG_CONTINUE

    def on_single_step(self, exception):
        """Called on requested single step ``exception`` is one of the following type:

                * :class:`windows.winobject.exception.EEXCEPTION_DEBUG_INFO32`
                * :class:`windows.winobject.exception.EEXCEPTION_DEBUG_INFO64`

        There is no default implementation, if you use ``Debugger.single_step()`` you should implement ``on_single_step``
        """
        raise NotImplementedError("Debugger that explicitly single step should implement <on_single_step>")

    def on_create_process(self, create_process):
        """Called on create_process event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms679286(v=vs.85).aspx)"""
        pass

    def on_exit_process(self, exit_process):
        """Called on exit_process event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms679334(v=vs.85).aspx)"""
        pass

    def on_create_thread(self, create_thread):
        """Called on create_thread event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms679287(v=vs.85).aspx)"""
        pass

    def on_exit_thread(self, exit_thread):
        """Called on exit_thread event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms679335(v=vs.85).aspx)"""
        pass

    def on_load_dll(self, load_dll):
        """Called on load_dll event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms680351(v=vs.85).aspx)"""
        pass

    def on_unload_dll(self, unload_dll):
        """Called on unload_dll event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms681403(v=vs.85).aspx)"""
        pass

    def on_output_debug_string(self, debug_string):
        """Called on debug_string event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms680545(v=vs.85).aspx)"""
        pass

    def on_rip(self, rip_info):
        """Called on rip_info event (for param type see https://msdn.microsoft.com/en-us/library/windows/desktop/ms680587(v=vs.85).aspx)"""
        pass