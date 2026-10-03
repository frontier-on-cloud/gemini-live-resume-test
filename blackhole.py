"""blackhole: silently drop one TCP flow inside a Linux network namespace (iptables).

Used by resume_test.py (round 3, scenarios B1 to B3) inside a container started with
--cap-add NET_ADMIN. For the flow local_ip:lport <-> remote_ip:rport, add() inserts

  OUTPUT: -p tcp -s local -d remote --sport lport --dport rport -j BH_OUT
  INPUT : -p tcp -s remote -d local --sport rport --dport lport -j BH_IN

Each chain counts RST and FIN segments in their own rule before a catch-all DROP, so
every dropped segment is counted exactly once. DROP sends nothing back (no RST, no
ICMP). The client kernel's own segments never leave the namespace; the server's
segments reach the interface (tcpdump sees them, the counters count them) and are
discarded before TCP, so they are never ACKed. Nothing is closed on either side. A new
connection has a new 4-tuple and passes. remove() deletes the two jumps: the old path
comes back.

FlowWatch runs `tcpdump --immediate-mode -l -n -tt -S -s 80` on the flow (headers
only, TLS payload is never decoded) and reports one dict per packet. On ingress tcpdump sees packets before
netfilter, on egress after it, so after add() any client-to-server line would be a leak.
ss_flow() returns the client socket as `ss -tinoH` shows it (state, timer, rto,
backoff, retrans, unacked, lastsnd, lastrcv, lastack).

No addresses other than the given flow are touched; cleanup() removes the jumps and
the two chains.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from typing import Any, Callable

CHAINS = {"in": "BH_IN", "out": "BH_OUT"}
COUNT_RULES = ("rst", "fin", "all")   # order of the rules in each chain


def _ipt(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(["iptables", "-w", *args], capture_output=True, text=True, timeout=10)
    if check and cp.returncode != 0:
        raise RuntimeError(f"iptables {' '.join(args)}: rc={cp.returncode} {cp.stderr.strip()[:200]}")
    return cp


def available() -> str | None:
    """None when iptables and tcpdump work here, else the reason."""
    if shutil.which("iptables") is None:
        return "iptables not installed"
    if shutil.which("tcpdump") is None:
        return "tcpdump not installed"
    cp = _ipt("-L", "OUTPUT", "-n", check=False)
    if cp.returncode != 0:
        return f"iptables not usable (NET_ADMIN?): {cp.stderr.strip()[:200]}"
    return None


class Blackhole:
    def __init__(self) -> None:
        self.flow: tuple[tuple[str, int], tuple[str, int]] | None = None
        self.active = False
        self.prepared = False
        self.version = _ipt("--version", check=False).stdout.strip()

    def _jumps(self) -> list[tuple[str, list[str]]]:
        (lip, lport), (rip, rport) = self.flow  # type: ignore[misc]
        return [("OUTPUT", ["-p", "tcp", "-s", lip, "-d", rip, "--sport", str(lport),
                            "--dport", str(rport), "-j", CHAINS["out"]]),
                ("INPUT", ["-p", "tcp", "-s", rip, "-d", lip, "--sport", str(rport),
                           "--dport", str(lport), "-j", CHAINS["in"]])]

    def prepare(self) -> None:
        """Create (or reset) the two counting chains; add() then only inserts jumps."""
        self._chains()
        self.prepared = True

    def _chains(self) -> None:
        for chain in CHAINS.values():
            if _ipt("-L", chain, "-n", check=False).returncode != 0:
                _ipt("-N", chain)
            _ipt("-F", chain)
            _ipt("-A", chain, "-p", "tcp", "--tcp-flags", "RST", "RST", "-j", "DROP")
            _ipt("-A", chain, "-p", "tcp", "--tcp-flags", "FIN", "FIN", "-j", "DROP")
            _ipt("-A", chain, "-j", "DROP")

    def add(self, local: tuple[str, int], remote: tuple[str, int]) -> dict:
        """Start dropping the flow both ways. Returns the rules and the monotonic time."""
        self.flow = (tuple(local[:2]), tuple(remote[:2]))  # type: ignore[assignment]
        if not self.prepared:
            self._chains()
        self.prepared = False   # the next add() starts from fresh counters
        for base, spec in self._jumps():
            _ipt("-I", base, "1", *spec)
        t, tw = time.monotonic(), time.time()
        self.active = True
        return {"t_mono": t, "t_wall": tw, "rules": [f"{b} {' '.join(s)}" for b, s in self._jumps()],
                "iptables": self.version}

    def remove(self) -> dict:
        """Stop dropping (the old path comes back). Chains and counters are kept."""
        out = []
        if self.flow is not None:
            for base, spec in self._jumps():
                while _ipt("-D", base, *spec, check=False).returncode == 0:
                    out.append(base)
        self.active = False
        return {"t_mono": time.monotonic(), "t_wall": time.time(), "removed": out}

    def counters(self) -> dict:
        """{"in": {"rst": [pkts, bytes], "fin": [...], "all": [...]}, "out": {...}}"""
        res: dict[str, dict[str, list[int]]] = {}
        for d, chain in CHAINS.items():
            cp = _ipt("-L", chain, "-v", "-n", "-x", check=False)
            rows = [ln.split() for ln in cp.stdout.splitlines()[2:] if ln.strip()]
            res[d] = {name: [int(r[0]), int(r[1])] for name, r in zip(COUNT_RULES, rows)}
        return res

    def cleanup(self) -> None:
        self.remove()
        for chain in CHAINS.values():
            if _ipt("-L", chain, "-n", check=False).returncode == 0:
                _ipt("-F", chain, check=False)
                _ipt("-X", chain, check=False)


# --------------------------------------------------------------------- watch --

_PKT = re.compile(
    r"^(?P<ts>\d+\.\d+) IP (?P<src>\d+\.\d+\.\d+\.\d+)\.(?P<sport>\d+) > "
    r"(?P<dst>\d+\.\d+\.\d+\.\d+)\.(?P<dport>\d+): Flags \[(?P<flags>[^\]]*)\]"
    r"(?:, seq (?P<seq>\d+)(?::(?P<seq_end>\d+))?)?(?:, ack (?P<ack>\d+))?"
    r"(?:, win (?P<win>\d+))?.*?(?:length (?P<len>\d+))?$")


def parse_tcpdump(line: str, remote_ip: str) -> dict | None:
    m = _PKT.match(line.strip())
    if not m:
        return None
    g = m.groupdict()
    return {"ts": float(g["ts"]), "dir": "s2c" if g["src"] == remote_ip else "c2s",
            "flags": g["flags"], "seq": int(g["seq"]) if g["seq"] else None,
            "seq_end": int(g["seq_end"]) if g["seq_end"] else None,
            "ack": int(g["ack"]) if g["ack"] else None,
            "win": int(g["win"]) if g["win"] else None,
            "len": int(g["len"]) if g["len"] else 0}


class FlowWatch:
    """tcpdump on one flow; calls on_packet(dict) for each parsed line."""

    def __init__(self, local: tuple[str, int], remote: tuple[str, int], iface: str,
                 on_packet: Callable[[dict], Any]) -> None:
        self.local, self.remote, self.iface = tuple(local[:2]), tuple(remote[:2]), iface
        self.on_packet = on_packet
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.unparsed = 0

    async def start(self, timeout: float = 5.0) -> str:
        expr = (f"tcp and host {self.remote[0]} and port {self.local[1]} "
                f"and port {self.remote[1]}")
        self.proc = await asyncio.create_subprocess_exec(
            "tcpdump", "-i", self.iface, "--immediate-mode", "-l", "-n", "-tt", "-S", "-s", "80",
            expr,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        banner = ""
        try:   # tcpdump prints "listening on ..." on stderr once the capture is live
            banner = (await asyncio.wait_for(self.proc.stderr.readline(), timeout)).decode().strip()
        except TimeoutError:
            banner = "no banner within timeout"
        self.task = asyncio.create_task(self._read())
        return banner

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            pkt = parse_tcpdump(line.decode(errors="replace"), self.remote[0])
            if pkt is None:
                self.unparsed += 1
                continue
            self.on_packet(pkt)

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 3)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self.task.cancel()


_SS_FIELDS = re.compile(r"\b(timer:\([^)]*\)|rto:\S+|backoff:\S+|retrans:\S+|unacked:\S+|"
                        r"lastsnd:\S+|lastrcv:\S+|lastack:\S+|bytes_acked:\S+|"
                        r"bytes_received:\S+|bytes_retrans:\S+|rtt:\S+)")


def ss_flow(local: tuple[str, int], remote: tuple[str, int]) -> dict:
    """The client socket of the flow as ss shows it ({"state": None} once it is gone)."""
    flt = f"( sport = :{local[1]} and dport = :{remote[1]} and dst {remote[0]} )"
    cp = subprocess.run(["ss", "-tinoH", "state", "all", flt], capture_output=True,
                        text=True, timeout=5)
    text = " ".join(cp.stdout.split())
    if not text:
        return {"state": None}
    out: dict[str, Any] = {"state": text.split()[0], "send_q": None, "recv_q": None}
    parts = text.split()
    if len(parts) > 2:
        out["recv_q"], out["send_q"] = parts[1], parts[2]
    for f in _SS_FIELDS.findall(text):
        k, _, v = f.partition(":")
        out[k] = v
    return out
