"""CloudWatch Embedded Metric Format: one JSON log line becomes a metric.
No PutMetricData calls, no extra latency, works from Lambda and from the API pods
(app logs) alike."""
import json
import time

NAMESPACE = "FrontRow"


def emit(name, value=1, service="api", unit="Count", **extra):
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [["Service"]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        "Service": service,
        name: value,
        **extra,
    }), flush=True)
