# Grafana / Prometheus monitoring

ai-autoedit exposes a Prometheus metrics endpoint at `/metrics`.

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: ai-autoedit
    metrics_path: /metrics
    static_configs:
      - targets:
          - IP:80
```

Replace `IP` with the host running the Docker stack.
Port 80 is intentionally exposed only for `/metrics` — nginx blocks everything else.

## Available metrics

| Metric | Type | Description |
|--------|------|-------------|
| `autoedit_jobs_total` | counter | Jobs started (label: `status` = done / failed) |
| `autoedit_jobs_running` | gauge | Jobs currently running |
| `autoedit_jobs_queued` | gauge | Jobs waiting in queue |
| `autoedit_render_duration_seconds` | histogram | Render duration in seconds |
| `autoedit_gpu_utilization_percent` | gauge | GPU utilization % (nvidia-smi) |
| `autoedit_gpu_memory_used_mb` | gauge | GPU VRAM used (MB) |
| `autoedit_gpu_temp_celsius` | gauge | GPU temperature (°C) |

> Metric names visible in the screenshot under `docs/grafana/`.

## Grafana dashboard

Import `docs/grafana/AI-autoedit-grafana-dashboard.json` via
**Dashboards → Import → Upload JSON file**.

Select your Prometheus data source when prompted.

## nginx port 80 (metrics-only)

Port 80 on the host is bound only to `/metrics`.
All other paths return 404 — no redirect to HTTPS, no app access.

```nginx
server {
    listen 80;
    location = /metrics {
        proxy_pass http://app/metrics;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
    }
    location / {
        return 404;
    }
}
```
