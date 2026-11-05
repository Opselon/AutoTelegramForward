package ops

import (
	"math"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Worker execution states (the contract's starting|ready|degraded|stopping).
const (
	StateStarting uint32 = iota
	StateReady
	StateDegraded
	StateStopping
)

// StateName maps an atomic state to its JSON name.
func StateName(s uint32) string {
	switch s {
	case StateReady:
		return "ready"
	case StateDegraded:
		return "degraded"
	case StateStopping:
		return "stopping"
	}
	return "starting"
}

// HeartbeatFreshnessBudget is how stale a worker's heartbeat may be before
// the process is considered NOT alive: the self-heartbeat ticks every 15s,
// so 45s is three missed ticks — enough to survive one slow poll or a
// momentary scheduler stall without flapping, short enough that a dead
// process shows offline within a minute.
const HeartbeatFreshnessBudget = 45 * time.Second

// derivedHealthState answers the console's "is the worker alive?" from the
// two things that can actually prove it: heartbeat freshness and the
// process's own execution state. It never reads a durable record — an old
// database row must not keep a worker "online" forever — and it never
// reports online for a process that cannot do its job (a degraded state or
// a dead queue store reads degraded, not online).
//
// age < 0 means "no heartbeat yet" (the process just started), which is
// starting, never offline: nothing has had a chance to go wrong.
func derivedHealthState(c *Collector, age int64) string {
	if age < 0 {
		return "starting"
	}
	if time.Duration(age)*time.Second > HeartbeatFreshnessBudget {
		return "offline"
	}
	switch c.State() {
	case StateDegraded:
		return "degraded"
	case StateStopping:
		return "stopping"
	case StateReady:
		if !c.DBConnected() {
			return "degraded" // alive but cannot reach its delivery store
		}
		return "online"
	default:
		return "starting"
	}
}

// runtimeStats reads the process's own runtime counters:
//
//   - goroutines: runtime.NumGoroutine. Reported as a DIAGNOSTIC, never as
//     utilization (the contract's explicit prohibition — a worker idling at
//     34 goroutines and delivering nothing would read "8.5x saturated").
//   - memory: runtime.ReadMemStats Sys — bytes the process obtained from
//     the OS, which is what an operator comparing against a container
//     memory limit actually wants to know.
func (c *Collector) runtimeStats() (goroutines int, memoryBytes uint64) {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	return runtime.NumGoroutine(), ms.Sys
}

// cpuStats reads process CPU time from /proc/self/stat.
//
// WHY /proc AND NOT Getrusage: both would be honest; /proc/self/stat is
// chosen because the fields are exactly utime+stime (kernel-reported
// time on CPU for THIS process, in clock ticks), it needs no cgo and no
// syscall wrapper debate, and it exists on every Linux the worker's
// container runs. This is a PROCESS measure (the contract's "process CPU
// since start (avg), not host CPU"), never a host-wide one: a host pinned
// at 90% by a neighbour must not paint this worker red.
//
// Non-Linux hosts (dev macOS/Windows): /proc is absent, so cpuStats returns
// zeros and /ops reports cpu_percent 0 rather than fabricating a number.
// The Dockerfile's runtime image is Alpine, so production always has it.
//
// Field indexing AFTER the closing paren of comm (man 5 proc):
// state=0 ppid=1 pgrp=2 session=3 tty=4 tpgid=5 flags=6 minflt=7 cminflt=8
// majflt=9 cmajflt=10 utime=11 stime=12 ...
func (c *Collector) cpuStats() (user, system time.Duration) {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return 0, 0
	}
	s := string(data)
	// comm is parenthesized and may contain spaces: skip past its closing
	// paren before whitespace-splitting the rest of the line.
	if i := strings.LastIndex(s, ")"); i >= 0 {
		s = s[i+1:]
	}
	fields := strings.Fields(s)
	if len(fields) < 13 {
		return 0, 0
	}
	u, err1 := strconv.ParseUint(fields[11], 10, 64)
	st, err2 := strconv.ParseUint(fields[12], 10, 64)
	if err1 != nil || err2 != nil {
		return 0, 0
	}
	// time.Duration is an int64 nanosecond count; the parsed utime/stime are
	// uint64 clock ticks. Cap the conversion rather than truncating: a value
	// above MaxInt64 ticks (~292 years at 100 Hz) is not representable and
	// must never wrap into a negative duration.
	const maxTicks = math.MaxInt64 / (int64(time.Second) / 100)
	if u > uint64(maxTicks) || st > uint64(maxTicks) {
		return 0, 0
	}
	const hz = 100 // USER_HZ on every supported Linux architecture
	return time.Duration(u) * time.Second / hz,
		time.Duration(st) * time.Second / hz
}

// cpuPercent returns the process's average CPU utilization since the
// collector was constructed: (user+system now − user+system at start) /
// elapsed. It is the contract's "process CPU since start (avg)", and it can
// exceed 100 on a multithreaded process (one core fully busy = 100).
func (c *Collector) cpuPercent() float64 {
	user, sys := c.cpuStats()
	if user == 0 && sys == 0 {
		return 0 // no /proc (non-Linux dev host): 0, never a fabricated value
	}
	elapsed := time.Since(c.cpuStart)
	if elapsed <= 0 {
		return 0
	}
	used := (user - c.cpuUser) + (sys - c.cpuSys)
	if used <= 0 {
		return 0
	}
	pct := float64(used) / float64(elapsed) * 100
	if pct < 0 {
		return 0
	}
	return round3(pct)
}
