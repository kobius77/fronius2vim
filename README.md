# fronius2vim

A Python-based tool that reads data from a Fronius PV inverter and writes it to VictoriaMetrics, with a built-in web dashboard for real-time visualization.

## Features

- **Dual-rate collection**: Real-time metrics every 10 seconds, energy counters every 15 minutes
- **VictoriaMetrics integration**: Native Prometheus protocol support
- **Web dashboard**: Real-time WebSocket updates with dark theme
- **Docker support**: Easy deployment with docker-compose
- **Proxmox helper script**: One-command installation on Proxmox VE

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

### Docker Compose

```bash
git clone https://github.com/kobius77/fronius2vim
cd fronius2vim
docker-compose up -d
```

Access the dashboard at http://localhost:8080

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONIUS_HOST` | `172.20.203.100` | Fronius inverter IP/hostname |
| `VICTORIAMETRICS_URL` | `http://172.20.204.22:8428` | VictoriaMetrics URL |
| `REALTIME_INTERVAL` | `10` | Real-time poll interval (seconds) |
| `ENERGY_INTERVAL` | `900` | Energy data poll interval (seconds) |
| `WEB_PORT` | `8080` | Web UI port |
| `LOG_LEVEL` | `INFO` | Logging level |

### Proxmox Installation (Recommended)

One-command automatic installation on Proxmox VE:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/kobius77/fronius2vim/main/install/fronius2vim.sh)"
```

This will:
- Create a Debian 12 LXC container
- Install Python and dependencies
- Clone and configure fronius2vim
- Start the service automatically
- No Docker required!

Access the dashboard at `http://<container-ip>:8080`

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
