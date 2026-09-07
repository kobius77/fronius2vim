# fronius2vim

A Python-based tool that polls real-time metrics and energy counters from a Fronius PV inverter and forwards them to VictoriaMetrics and an MQTT broker, complete with a live web dashboard.

## Features

- **Dual-rate collection**: Real-time power metrics every 10 seconds (configurable), energy counters every 15 minutes.
- **MQTT Publishing**: Automatically publishes live AC power output in Watts (`W`) as a plain numeric string (e.g. `2500.0`, `0.0` at night/standby) with configurable retain flag and QoS.
- **VictoriaMetrics Integration**: Native Prometheus line protocol export for power and daily/yearly/total energy metrics.
- **Web Dashboard**: Modern, responsive dark-themed dashboard with live WebSocket updates, today's generation vs. power chart, 7-day historical summary, and real-time VictoriaMetrics & MQTT connection indicators.
- **Broad Fronius Inverter Compatibility**: Supports Fronius Symo, Primo, SnapINverter, and Generation 24 / Tauro inverters with robust handling of standby/night mode.
- **Docker Support & Auto-Updates**: Simple `docker compose` setup with Watchtower support for automatic container redeployment on new git releases.

## Architecture

```
                       ┌─────────────────────────┐
                       │  Fronius Solar API      │
                       │  (Inverter / Datamanager)
                       └───────────┬─────────────┘
                                   │
                           HTTP Poll (10s / 15m)
                                   │
                                   ▼
                       ┌─────────────────────────┐
                       │       fronius2vim       │
                       └─────┬────────┬────────┬─┘
                             │        │        │
             Prometheus Write│   MQTT │        │ WebSocket (Live)
                             │        │        │
                             ▼        ▼        ▼
                   VictoriaMetrics   MQTT    Web UI
                                    Broker  Dashboard
```

## Metrics & Outputs

### 1. MQTT
- **Topic**: `froniusalt/power` (configurable via `MQTT_TOPIC`)
- **Payload**: Plain numeric string in Watts (e.g., `2500.0` or `0.0`)
- **Retain**: `true` by default (stores the latest power state in the broker for new subscribers)

### 2. VictoriaMetrics (Prometheus Protocol)
- `fronius_power_watts` - Current AC power output (Watts, polled every `REALTIME_INTERVAL`)
- `fronius_daily_energy_watthours` - Current day energy production (Wh, polled every `ENERGY_INTERVAL`)
- `fronius_yearly_energy_watthours` - Current year energy production (Wh, polled every `ENERGY_INTERVAL`)
- `fronius_total_energy_watthours` - Total lifetime energy production (Wh, polled every `ENERGY_INTERVAL`)

---

## Quick Start

### Docker Compose (Recommended)

```bash
git clone https://github.com/kobius77/fronius2vim
cd fronius2vim
docker compose up -d
```

Access the dashboard at `http://localhost:8080`.

### Automatic Updates with Watchtower (Optional)

The `docker-compose.yml` includes **Watchtower** for automatic redeployment when new images are published:

```bash
# Create .env file with GitHub credentials
cp .env.example .env

# Edit .env with your GitHub username and Personal Access Token (scope: read:packages)
nano .env

# Start services (includes watchtower)
docker compose up -d
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed setup instructions.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONIUS_HOST` | `172.20.203.100` | Fronius inverter IP/hostname |
| `VICTORIAMETRICS_URL` | `http://172.20.204.22:8428` | VictoriaMetrics base URL |
| `REALTIME_INTERVAL` | `10` | Real-time poll & publish interval (seconds) |
| `ENERGY_INTERVAL` | `900` | Energy counters poll interval (seconds, 900s = 15m) |
| `WEB_PORT` | `8080` | Web UI port |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `MQTT_HOST` | _(empty/disabled)_ | MQTT Broker IP/hostname (enables MQTT when set) |
| `MQTT_PORT` | `1883` | MQTT Broker port |
| `MQTT_TOPIC` | `froniusalt/power` | MQTT topic for produced power in Watts |
| `MQTT_USERNAME` | _(empty)_ | MQTT username (optional) |
| `MQTT_PASSWORD` | _(empty)_ | MQTT password (optional) |
| `MQTT_CLIENT_ID` | `fronius2vim` | MQTT client identifier |
| `MQTT_RETAIN` | `true` | Retain flag for published MQTT messages (`true`/`false`) |
| `MQTT_QOS` | `0` | MQTT QoS level (0, 1, or 2) |

---

## API Endpoints

- `GET /` - Web dashboard
- `GET /api/data` - Current metrics & MQTT status (JSON)
- `GET /api/today` - 24-hour combined hourly energy and 15m power history (from VictoriaMetrics)
- `GET /api/history/7days` - 7-day daily energy generation history (from VictoriaMetrics)
- `GET /api/metrics-log` - Last 50 metric writes to VictoriaMetrics
- `WS /ws` - WebSocket for real-time live dashboard updates

---

## VictoriaMetrics Query Examples

```promql
# Current power in Watts
fronius_power_watts

# Daily energy trend
fronius_daily_energy_watthours

# Average power over 1 hour
avg_over_time(fronius_power_watts[1h])
```

---

## License

MIT
