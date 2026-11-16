from prometheus_client import Counter, Gauge

ORDERS_CREATED = Counter(
    "orders_created_total",
    "Number of created orders",
    ["notification_status"],
)
UPSTREAM_FAILURES = Counter(
    "upstream_call_failures_total",
    "Failed calls to downstream services",
    ["service"],
)
CIRCUIT_OPEN = Gauge(
    "circuit_breaker_open",
    "1 if the circuit breaker for the downstream service is open",
    ["service"],
)
