"""
RuntimeProcess: runs a DynoRuntime in its own process, so the GUI cannot slow it down.

    control/runtime.py          the control loop itself (DynoRuntime): single-threaded,
                                knows nothing about processes or GUIs
    control/runtime_process.py  runs that loop in a separate "control process" and gives
                                the GUI a handle to it (RuntimeProcess: start / start_run /
                                request_stop / poll / close / active, callbacks)

WHY A SEPARATE PROCESS
    CPython lets only one thread of a process run Python code at a time (the GIL).
    With the GUI and the loop as two threads of one process, every plot redraw that
    held the GIL delayed the loop: a Modbus reply would arrive, but the loop could not
    process it - or timestamp it - until the GUI let go. Cycles stretched beyond the
    100 ms budget and encoder timestamps jittered, which showed up as RPM noise. Each
    process has its own interpreter and its own GIL, and the operating system runs them
    in parallel, so nothing the GUI does can delay a Modbus transaction or a timestamp
    in the control process.

LAYOUT AND FLOW
    GUI process (main)                              control process (child)
    ------------------                              -----------------------
    RuntimeProcess.start()  -- spawns -------->     _control_process_main()
                            <-- "ready"/"error" --    opens the drive (vfd_factory),
                                                      DynoRuntime.start(): 0 N·m + STOP,
                                                      torque scaling check
    start_run / request_stop
                            -- commands queue -->     DynoRuntime.run() = the loop, in the
                                                      main thread: picks up waiting commands
                                                      at the start of every cycle, then
                                                      Modbus I/O, computation, CSV log
    poll() (GUI timer)      <-- events queue ----     the loop's callbacks put Sample /
      -> on_sample / on_message / on_run_finished     message / RunResult on the queue
         called in the GUI thread
    close()                 -- "exit" ---------->     run() returns after ramp -> 0 N·m ->
                            <-- "closed" --------     STOP; close the port, report, exit

WHY NOTHING IN THE CONTROL PROCESS COMPETES WITH THE LOOP
    The control process runs two threads:
      - the main thread: the loop (DynoRuntime.run()). It also reads the commands
        queue, without blocking, at the start of each cycle (_CommandSource) - a few
        microseconds when nothing is waiting.
      - the events queue's feeder thread (multiprocessing internal): pickles each event
        and writes it to the pipe. The loop's callbacks only append the event to an
        in-memory buffer, so the loop never waits on the pipe or on the GUI. Pickling
        one Sample takes microseconds; the RunResult at the end of a run takes longer,
        but the loop is then back in IDLE.
    Neither does GUI work or anything that grows with run length, so the loop gets the
    interpreter whenever it needs it. If the GUI falls behind or freezes, events
    simply wait in the queue: the loop and the CSV log carry on at full rate.

OTHER EFFECTS OF THE SPLIT
    - The serial port exists only in the control process; the GUI process cannot
      touch the drive.
    - The CSV log is written in the control process, next to the data: a slow or
      frozen GUI cannot cost logged data.
    - Ctrl+C reaches both processes (same terminal). The control process ignores it,
      so only the GUI reacts and shutdown goes through the normal path.
    - If the GUI process dies, the control process notices within PARENT_CHECK_S, runs
      the normal shutdown (ramp -> 0 N·m -> STOP) and exits, instead of leaving the
      drive at its last torque. If the control process dies, poll() reports it once.
    - The loop now talks to the drive every cycle whatever the GUI does, which makes
      the drive's comm-loss timeout (Pr.09-03) safe to enable.
    - Everything crossing the boundary is pickled: profiles, Sample, RunResult (all
      plain data). The drive is created inside the control process by vfd_factory (a
      picklable callable, default C2000.connect).
    - The "spawn" start method is used on every OS: the child is a fresh interpreter
      (fork would copy the GUI process's Qt state). It re-imports the launching
      script's modules, so startup takes ~1-2 s longer, and the launching script must
      keep its top-level code under `if __name__ == "__main__":`.
    - Always call close(): a non-daemonic child keeps the GUI process from exiting.
"""

import multiprocessing as mp
import queue
import signal
import time

from control.runtime import IDLE, DynoRuntime, shutdown_timeout_s
from hardware.c2000 import C2000

START_TIMEOUT_S = 30.0    # child startup: new interpreter + imports + port + 0 N·m/STOP + scaling check
PARENT_CHECK_S = 0.5      # how often the control process checks that the GUI process is alive
EXIT_MARGIN_S = 5.0       # extra wait for the control process to exit after its runtime has closed
WAIT_SLICE_S = 0.2        # blocking waits in the GUI process are sliced to notice a dead child


# ==============================================================================
# GUI process side
# ==============================================================================
class RuntimeProcess:
    """
    Handle to a DynoRuntime running in the control process. The owner must call
    poll() regularly (DynoWindow does from its GUI timer): callbacks only run inside
    poll(), in the caller's thread.
    """

    def __init__(self, vfd_factory=C2000.connect, on_sample=None, on_message=None, on_run_finished=None):
        # Same callbacks as DynoRuntime's, but called from poll() in the owner's thread
        # (the GUI thread), so they may update widgets directly.
        self.on_sample = on_sample or (lambda sample: None)
        self.on_message = on_message or (lambda text: None)
        self.on_run_finished = on_run_finished or (lambda result: None)

        self.phase = IDLE          # last runtime phase reported by the control process
        self._finished = False     # control process has ended (closed, failed or died)

        ctx = mp.get_context("spawn")
        self._commands = ctx.Queue()     # GUI -> control: (kind, arg)
        self._events = ctx.Queue()       # control -> GUI: (kind, payload)
        self._process = ctx.Process(
            target=_control_process_main,
            args=(vfd_factory, self._commands, self._events),
            name="dyno-control",
        )

    # --------------------------------------------------------------------------
    # Controls (all return at once; the control process acts on them in order)
    # --------------------------------------------------------------------------
    def start(self):
        """
        Step 1: starts the control process and waits until its runtime is up: drive
        connected, 0 N·m + STOP sent, torque scaling verified. Raises RuntimeError with
        the control process's error message if any of that fails.
        """
        self._process.start()
        kind, detail = self._wait_for_event(("ready", "error"), START_TIMEOUT_S)
        if kind != "ready":
            self._process.join(EXIT_MARGIN_S)
            self._finished = True
            raise RuntimeError(detail or f"control process gave no answer within {START_TIMEOUT_S:g} s")

    def start_run(self, profile, save_log=True):
        """Step 2: queues a ("start", ...) command for DynoRuntime.run() in the control process."""
        self._commands.put(("start", (profile, save_log)))

    def request_stop(self, reason="Stop requested"):
        """Queues a ("stop", reason) command for DynoRuntime.run() in the control process."""
        self._commands.put(("stop", reason))

    def poll(self):
        """
        Step 3: delivers everything the control process has sent since the last call,
        in order, by calling the callbacks in the caller's thread. Never blocks.
        """
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "sample":
                self.phase = payload.phase
                self.on_sample(payload)
            elif kind == "message":
                self.on_message(payload)
            elif kind == "run_finished":
                self.phase = IDLE
                self.on_run_finished(payload)
        # Checked after draining, so a dying process's last messages are shown first.
        if not self._finished and not self._process.is_alive():
            self._finished = True
            self.phase = IDLE
            self.on_message("[ERROR] The control process stopped unexpectedly - "
                            "check the drive and use keypad STOP.")

    def close(self, timeout=None):
        """
        Step 4: asks the control process to shut down through the normal path (any
        active run: ramp -> 0 N·m -> STOP; then the port is closed) and waits for it.
        Returns True if the runtime reported a clean shutdown and the process exited.
        Messages sent meanwhile still go to on_message; samples are dropped.
        """
        if self._finished:
            return False
        self._commands.put(("exit", None))
        kind, clean = self._wait_for_event(("closed",), timeout or shutdown_timeout_s() + EXIT_MARGIN_S)
        self._process.join(EXIT_MARGIN_S)
        self._finished = True
        return kind == "closed" and clean and not self._process.is_alive()

    @property
    def active(self):
        return self.phase != IDLE

    # --------------------------------------------------------------------------
    def _wait_for_event(self, kinds, timeout):
        """
        Blocks until an event of one of `kinds` arrives; returns (kind, payload), or
        (None, None) on timeout or if the control process ends first. Messages that
        arrive meanwhile go to on_message. Reading the queue here is also what lets the
        control process exit: a process that has put data on a multiprocessing queue
        does not end until that data has been read.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                kind, payload = self._events.get(timeout=WAIT_SLICE_S)
            except queue.Empty:
                if not self._process.is_alive():
                    break
                continue
            if kind in kinds:
                return kind, payload
            if kind == "message":
                self.on_message(payload)
        return None, None


# ==============================================================================
# Control process side
# ==============================================================================
def _control_process_main(vfd_factory, commands, events):
    """
    Entry point of the control process (runs in the child; must stay a module-level
    function so "spawn" can import it). Owns the drive and the DynoRuntime for the
    whole session.
    """
    # Ctrl+C reaches both processes; only the GUI process acts on it (it then closes
    # the window, which sends "exit" through the normal shutdown path below).
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    # Step 1 (child): open the drive and start the runtime, then report back.
    try:
        vfd = vfd_factory()
    except Exception as e:
        events.put(("error", f"Could not open the drive connection: {e}"))
        return
    # The runtime's callbacks run in the loop. Queue.put only appends to an in-memory
    # buffer and returns; pickling and sending happen in the queue's feeder thread, so
    # the loop never waits for the GUI process.
    runtime = DynoRuntime(
        vfd,
        on_sample=lambda sample: events.put(("sample", sample)),
        on_message=lambda text: events.put(("message", text)),
        on_run_finished=lambda result: events.put(("run_finished", result)),
    )
    try:
        runtime.start()                  # 0 N·m + STOP, scaling check (the loop starts below)
    except Exception as e:
        events.put(("error", str(e)))
        vfd.close()
        return
    events.put(("ready", None))

    # Step 2 (child): the loop runs right here, in this process's main thread. Commands
    # from the GUI are picked up at the start of every cycle through command_source.
    command_source = _CommandSource(commands)
    clean = runtime.run(command_source.next)

    # Step 4 (child): run() has returned after the normal shutdown (any active run
    # ramped to 0 N·m, then STOP) or after giving up on it. Close the port and report.
    vfd.close()
    if command_source.gui_alive:
        events.put(("closed", clean))
    else:
        events.cancel_join_thread()      # nobody reads the queue any more: do not wait to flush it


class _CommandSource:
    """
    Feeds the GUI's commands to DynoRuntime.run(), which calls next() at the start of
    every cycle until it returns None. next() never blocks: get_nowait() on the
    commands queue costs microseconds when nothing is waiting. Every PARENT_CHECK_S it
    also checks that the GUI process is still alive; if not, it hands out one
    ("exit", None), so a dead GUI goes through the same normal shutdown as a closed
    window, and the control process does not leave the drive at its last torque.
    """

    def __init__(self, commands):
        self._commands = commands
        self._parent = mp.parent_process()
        self._next_check = time.monotonic() + PARENT_CHECK_S
        self.gui_alive = True

    def next(self):
        if not self.gui_alive:
            return None                  # the exit has already been handed out
        if time.monotonic() >= self._next_check:
            self._next_check = time.monotonic() + PARENT_CHECK_S
            if not self._parent.is_alive():
                self.gui_alive = False
                return ("exit", None)
        try:
            return self._commands.get_nowait()
        except queue.Empty:
            return None
