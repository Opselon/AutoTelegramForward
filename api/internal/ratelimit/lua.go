package ratelimit

// The Lua sources below are the algorithms. Everything about them is chosen so
// that one limit check is exactly ONE round trip and ONE key:
//
//   - The refill, the decision and the state update happen inside Redis, so N
//     API replicas sharing a bucket cannot overshoot each other's reads (a
//     read-modify-write from the client would need WATCH/MULTI and two round
//     trips, and would still lose under contention).
//   - Every write carries a TTL, so a burst of distinct keys cannot grow the
//     keyspace without bound: a bucket dies once it would have refilled, a
//     window dies once it can no longer influence a decision.
//   - Time is passed in by the caller (milliseconds) instead of read from the
//     server clock, so a replica that restarts mid-window cannot resurrect an
//     old bucket: the timestamp it wrote is monotonic per process, and skew
//     between replicas is clamped to "never mint tokens".

// bucketScript is the atomic token bucket.
//
//	KEYS[1] bucket key
//	ARGV[1] capacity            (tokens, > 0)
//	ARGV[2] refill rate         (tokens per second, > 0)
//	ARGV[3] now                 (unix milliseconds)
//	ARGV[4] cost                (tokens this request spends)
//	ARGV[5] ttl                 (milliseconds)
//
// returns {allowed, remaining, retry_after_ms}
//
// Fractional tokens are kept (a limit of 100/minute refills ~0.0017 tokens per
// millisecond; rounding down on every write would make the bucket leak most of
// its refill). They are formatted with a bounded precision so the stored value
// stays short.
const bucketScript = `
local key  = KEYS[1]
local cap  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now  = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl  = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 't', 'l')
local tokens = tonumber(data[1])
local last   = tonumber(data[2])

if tokens == nil then
  -- First sighting of this key: the caller starts with a full bucket, which is
  -- what "100 requests per minute" means to a user who has been quiet.
  tokens = cap
  last = now
elseif last == nil then
  last = now
end

local elapsed = now - last
if elapsed < 0 then elapsed = 0 end -- clock skew between replicas: never mint tokens
if elapsed > 0 then
  local refill = (elapsed * rate) / 1000.0
  tokens = math.min(cap, tokens + refill)
end

local allowed = 0
local retry = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  -- Time until the bucket holds the cost again. Reported so the client can be
  -- told Retry-After without a second round trip.
  retry = math.ceil(((cost - tokens) * 1000.0) / rate)
  if retry < 1 then retry = 1 end
end

redis.call('HSET', key, 't', string.format('%.6f', tokens), 'l', now)
redis.call('PEXPIRE', key, ttl)

return {allowed, math.floor(tokens), retry}
`

// windowScript is the bounded sliding-window counter.
//
// It keeps two time buckets per key (the current one and the one before it) and
// weights the older bucket by the part of the window it still covers, which is
// the standard approximation of a true sliding window: at most one window of
// history, two integers of memory, and one round trip.
//
//	KEYS[1] window key
//	ARGV[1] limit       (requests per window)
//	ARGV[2] window      (milliseconds)
//	ARGV[3] now         (unix milliseconds)
//	ARGV[4] cost
//	ARGV[5] ttl         (milliseconds)
//
// returns {allowed, remaining, retry_after_ms}
const windowScript = `
local key    = KEYS[1]
local limit  = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])
local cost   = tonumber(ARGV[4])
local ttl    = tonumber(ARGV[5])

local now_bucket = math.floor(now / window)
local data = redis.call('HMGET', key, 'b', 'c', 'p')
local bucket = tonumber(data[1])
local cur    = tonumber(data[2])
local prev   = tonumber(data[3])

if bucket == nil or cur == nil then
  bucket, cur, prev = now_bucket, 0, 0
elseif bucket ~= now_bucket then
  -- Rolling the key forward: the bucket that was current becomes the previous
  -- one; anything older than that has left the window and is dropped.
  if bucket == now_bucket - 1 then prev = cur else prev = 0 end
  bucket, cur = now_bucket, 0
end
if prev == nil then prev = 0 end

local elapsed = now - now_bucket * window
local weight  = (window - elapsed) / window -- share of the previous bucket still in the window
local estimate = prev * weight + cur

local allowed = 0
local retry = 0
if estimate + cost > limit then
  retry = math.ceil(window - elapsed) -- the previous bucket is fully out after this
  if prev > 0 then
    -- The weighted previous bucket decays linearly, so the wait for the missing
    -- units to leave the window is missing*window/prev.
    local need = estimate + cost - limit
    local decay = math.ceil((need * window) / prev)
    if decay > 0 and decay < retry then retry = decay end
  end
  if retry < 1 then retry = 1 end
else
  allowed = 1
  cur = cur + cost
  estimate = estimate + cost
end

redis.call('HSET', key, 'b', bucket, 'c', cur, 'p', prev)
redis.call('PEXPIRE', key, ttl)

local remaining = limit - math.floor(estimate)
if remaining < 0 then remaining = 0 end
return {allowed, remaining, retry}
`

// exactWindowScript is the reference implementation: a genuine sliding window
// over a sorted set, with one member per admitted request.
//
// It exists to MEASURE the approximation (accuracy under burst traffic) rather
// than to be used in production — it stores one sorted-set member per request
// instead of two integers per key, so its memory grows with the traffic in the
// window. The benchmark and the accuracy test compare it against windowScript;
// the cheaper approximation is the default because the difference in decisions
// is a fraction of one request at the bucket boundary.
//
// KEYS[1] key; ARGV[1] limit, ARGV[2] window ms, ARGV[3] now ms, ARGV[4] cost,
// ARGV[5] ttl ms, ARGV[6] unique member prefix.
const exactWindowScript = `
local key    = KEYS[1]
local limit  = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])
local cost   = tonumber(ARGV[4])
local ttl    = tonumber(ARGV[5])
local prefix = ARGV[6]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local used = redis.call('ZCARD', key)

local allowed = 0
local retry = 0
if used + cost <= limit then
  allowed = 1
  for i = 1, cost do
    redis.call('ZADD', key, now, prefix .. ':' .. now .. ':' .. i .. ':' .. math.random(1, 1000000000))
  end
  used = used + cost
else
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  if oldest[2] then
    retry = math.ceil((tonumber(oldest[2]) + window) - now)
  else
    retry = window
  end
  if retry < 1 then retry = 1 end
end

redis.call('PEXPIRE', key, ttl)
local remaining = limit - used
if remaining < 0 then remaining = 0 end
return {allowed, remaining, retry}
`

// leaseRenewScript extends a lease only for the process that owns it.
//
//	KEYS[1] lease key; ARGV[1] token; ARGV[2] ttl ms
//	returns 1 when renewed, 0 when the lease is gone or owned by someone else
//
// The token check is the whole point: without it, a process that stalled past
// its lease would "renew" a lease another process has since acquired, and two
// replicas would run the singleton job at once.
const leaseRenewScript = `
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
`

// leaseReleaseScript releases a lease only for its owner, so a process that
// lost the lease cannot delete the winner's.
//
//	KEYS[1] lease key; ARGV[1] token
const leaseReleaseScript = `
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
`

// luaScripts are the registered scripts, in the order they are loaded at
// startup (one SCRIPT LOAD per script per process, then EVALSHA forever).
var luaScripts = struct {
	Bucket      scriptSource
	Window      scriptSource
	ExactWindow scriptSource
	LeaseRenew  scriptSource
	Lease       scriptSource
}{
	Bucket:      scriptSource{Name: "bucket", Body: bucketScript},
	Window:      scriptSource{Name: "window", Body: windowScript},
	ExactWindow: scriptSource{Name: "window_exact", Body: exactWindowScript},
	LeaseRenew:  scriptSource{Name: "lease_renew", Body: leaseRenewScript},
	Lease:       scriptSource{Name: "lease_release", Body: leaseReleaseScript},
}

type scriptSource struct {
	Name string
	Body string
}
