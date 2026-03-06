# fronius2vim

A Python-based tool that reads data from a Fronius PV inverter and writes it to VictoriaMetrics, with a built-in web dashboard for real-time visualization.

## Features

- **Dual-rate collection**: Real-time metrics every 10 seconds, energy counters every 15 minutes
- **VictoriaMetrics integration**: Native Prometheus protocol support
- **Web dashboard**: Real-time WebSocket updates with dark theme
- **Docker support**: Easy deployment with docker-compose
- **Automatic updates**: Watchtower auto-redeploys on new commits

## Metrics Collected

### Real-time (10s interval)
- `fronius_power_watts` - Current AC power output
- `fronius_ac_voltage_volts` - AC voltage
- `fronius_dc_voltage_volts` - DC voltage
- `fronius_ac_current_amperes` - AC current
- `fronius_dc_current_amperes` - DC current

### Energy (15min interval)
- `fronius_daily_energy_watthours` - Daily energy production
- `fronius_yearly_energy_watthours` - Yearly energy production
- `fronius_total_energy_watthours` - Total lifetime energy

## Quick Start

### Docker Compose (Recommended)

```bash
git clone https://github.com/kobius77/fronius2vim
cd fronius2vim
docker-compose up -d
```

Access the dashboard at http://localhost:8080

### Automatic Updates (Optional)

The docker-compose includes **Watchtower** for automatic redeployment when new images are published:

```bash
# Create .env file with GitHub credentials
cp .env.example .env
# Edit .env with your GitHub username and Personal Access Token
nano .env

# Start services (includes watchtower)
docker-compose up -d
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed setup instructions.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONIUS_HOST` | `172.20.203.100` | Fronius inverter IP/hostname |
| `VICTORIAMETRICS_URL` | `http://172.20.204.22:8428` | VictoriaMetrics URL |
| `REALTIME_INTERVAL` | `10` | Real-time poll interval (seconds) |
| `ENERGY_INTERVAL` | `900` | Energy data poll interval (seconds) |
| `WEB_PORT` | `8080` | Web UI port |
| `LOG_LEVEL` | `INFO` | Logging level |

## API Endpoints

- `GET /` - Web dashboard
- `GET /api/data` - Current metrics (JSON)
- `WS /ws` - WebSocket for real-time updates

## VictoriaMetrics Query Examples

```promql
# Current power
fronius_power_watts

# Daily energy trend
fronius_daily_energy_watthours

# Average power over 1 hour
avg_over_time(fronius_power_watts[1h])
```

## License

MIT
