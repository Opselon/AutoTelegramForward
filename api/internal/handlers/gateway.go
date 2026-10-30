package handlers

import (
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"

	pb "autoforward/proto"
)

// GatewayInfo answers GET /api/gateway — the DevOps panel's system summary.
//
// The dashboard has polled this route every ten seconds since the panel was
// built, but the route was never registered: every poll answered 404, the panel
// fell back to a hardcoded version, and the access log filled with
// "Not Found GET /api/gateway". It is a thin, tenant-scoped composition of data
// /stats and /health/services already expose, so nothing here is invented.
func (h *Handlers) GatewayInfo(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := withTimeout(r)
	defer cancel()

	id := identity(r)
	// The version is the *product* version everywhere else in the API
	// (/api/stats, /api/system/setup, /api/version). This route used to leave
	// it to the core's answer, so the dashboard's header chip could read a core
	// build string while the System panel read the product release — two
	// versions on one screen, neither labelled. Seed it from the same resolver
	// and let the core override only when it reports one.
	out := map[string]any{
		"atf_core_online":   false,
		"atf_logger_online": h.c.Logs != nil,
		"atf_web_online":    true,
		"uptime_seconds":    0,
		"grpc_port":         addrPort(os.Getenv("ATF_CORE_GRPC"), 50051),
		"logger_port":       addrPort(os.Getenv("ATF_LOGGER_ADDR"), 50052),
		"web_port":          addrPort(os.Getenv("ATF_WEB_ADDR"), 8088),
		"version":           resolveProductVersion(),
		"db_size_bytes":     0,
		"total_rules":       0,
		"total_sessions":    0,
	}

	if h.c.System != nil {
		if resp, err := h.c.System.GetSystemStats(ctx, &pb.SystemStatsRequest{OwnerUserId: id.UserID}); err == nil {
			out["atf_core_online"] = resp.CoreRunning
			out["uptime_seconds"] = resp.UptimeSeconds
			out["total_rules"] = resp.ActiveRulesCount
			out["total_sessions"] = resp.ActiveSessionsCount
		}
	}
	writeJSON(w, http.StatusOK, out)
}

// addrPort returns the port from a "host:port" address, or the fallback when
// the address is empty or does not carry one. The panel only renders the value,
// so a wrong default must never be reported as a real listening port.
func addrPort(addr string, fallback int) int {
	addr = strings.TrimSpace(addr)
	if addr == "" {
		return fallback
	}
	if _, port, err := net.SplitHostPort(addr); err == nil {
		if n, convErr := strconv.Atoi(port); convErr == nil && n > 0 {
			return n
		}
	}
	if n, err := strconv.Atoi(addr); err == nil && n > 0 {
		return n
	}
	return fallback
}
