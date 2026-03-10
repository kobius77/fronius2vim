#!/usr/bin/env python3
"""
fronius2vim - Fronius Inverter to VictoriaMetrics Collector
Polls Fronius Solar API and writes metrics to VictoriaMetrics
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# Configuration
# Default IPs hardcoded for this deployment
FRONIUS_HOST = os.getenv("FRONIUS_HOST", "172.20.203.100")
VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://172.20.204.22:8428")
REALTIME_INTERVAL = int(os.getenv("REALTIME_INTERVAL", "10"))
ENERGY_INTERVAL = int(os.getenv("ENERGY_INTERVAL", "900"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("fronius2vim")

# FastAPI app
app = FastAPI(title="fronius2vim", version="1.0.0")

# Latest data cache for WebSocket
latest_data: Dict[str, Any] = {
    "power": 0,
    "daily_energy": 0,
}

# Log of recent metrics written to VictoriaMetrics (for dashboard)
metrics_log: list = []
MAX_LOG_ENTRIES = 50


class FroniusCollector:
    """Collects data from Fronius inverter API"""

    def __init__(self, host: str):
        self.host = host
        self.base_url = f"http://{host}/solar_api/v1"
        self.client = httpx.AsyncClient(timeout=10.0)

    async def get_realtime_data(self) -> Optional[Dict]:
        """Get real-time inverter data (PAC power only)"""
        url = f"{self.base_url}/GetInverterRealtimeData.cgi"
        params = {"Scope": "System", "DataCollection": "CumulationInverterData"}

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            body = data.get("Body", {}).get("Data", {})

            # Sum values from all inverters
            def sum_all_inverters(metric_dict):
                values = metric_dict.get("Values", {})
                return sum(float(v) for v in values.values())

            return {
                "power": sum_all_inverters(body.get("PAC", {})),
            }
        except Exception as e:
            logger.error(f"Failed to get realtime data: {e}")
            return None

    async def get_energy_data(self) -> Optional[Dict]:
        """Get energy counters (DAY, YEAR, TOTAL)"""
        url = f"{self.base_url}/GetInverterRealtimeData.cgi"
        params = {"Scope": "System", "DataCollection": "CumulationInverterData"}

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            body = data.get("Body", {}).get("Data", {})

            # Sum values from all inverters
            def sum_all_inverters(metric_dict):
                values = metric_dict.get("Values", {})
                return sum(float(v) for v in values.values())

            return {
                "daily": sum_all_inverters(body.get("DAY_ENERGY", {})),
                "yearly": sum_all_inverters(body.get("YEAR_ENERGY", {})),
                "total": sum_all_inverters(body.get("TOTAL_ENERGY", {})),
            }
        except Exception as e:
            logger.error(f"Failed to get energy data: {e}")
            return None


class VictoriaMetricsWriter:
    """Writes metrics to VictoriaMetrics using Prometheus remote write"""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=10.0)

    async def write_metric(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None
    ):
        """Write a single metric to VictoriaMetrics"""
        global metrics_log
        if labels is None:
            labels = {}

        # Build Prometheus format: metric_name{label1="value1"} value timestamp
        label_str = ",".join([f'{k}="{v}"' for k, v in labels.items()])
        if label_str:
            metric_line = f"{name}{{{label_str}}} {value}"
        else:
            metric_line = f"{name} {value}"

        import_url = f"{self.url}/api/v1/import/prometheus"

        try:
            response = await self.client.post(
                import_url,
                content=metric_line.encode(),
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()

            # Log to dashboard metrics log
            entry = {
                "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
                "metric": name,
                "value": value,
                "labels": labels,
                "status": "success",
            }
            metrics_log.insert(0, entry)
            if len(metrics_log) > MAX_LOG_ENTRIES:
                metrics_log = metrics_log[:MAX_LOG_ENTRIES]

            logger.debug(f"Wrote metric: {metric_line}")
        except Exception as e:
            # Log failure
            entry = {
                "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
                "metric": name,
                "value": value,
                "labels": labels,
                "status": "error",
                "error": str(e),
            }
            metrics_log.insert(0, entry)
            if len(metrics_log) > MAX_LOG_ENTRIES:
                metrics_log = metrics_log[:MAX_LOG_ENTRIES]

            logger.error(f"Failed to write metric to VictoriaMetrics: {e}")

    async def write_realtime_metrics(self, data: Dict):
        """Write real-time metrics"""
        await self.write_metric(
            "fronius_power_watts", data["power"], {"inverter": "system"}
        )

    async def write_energy_metrics(self, data: Dict):
        """Write energy counter metrics"""
        await self.write_metric(
            "fronius_daily_energy_watthours", data["daily"], {"inverter": "system"}
        )
        await self.write_metric(
            "fronius_yearly_energy_watthours", data["yearly"], {"inverter": "system"}
        )
        await self.write_metric(
            "fronius_total_energy_watthours", data["total"], {"inverter": "system"}
        )


async def realtime_collector(
    collector: FroniusCollector, writer: VictoriaMetricsWriter
):
    """Background task: collect real-time data every 10 seconds"""
    while True:
        try:
            data = await collector.get_realtime_data()
            if data:
                await writer.write_realtime_metrics(data)
                # Update cache for WebSocket
                latest_data["power"] = data["power"]
                latest_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"Realtime data: Power={data['power']}W")
        except Exception as e:
            logger.error(f"Error in realtime collector: {e}")

        await asyncio.sleep(REALTIME_INTERVAL)


async def energy_collector(collector: FroniusCollector, writer: VictoriaMetricsWriter):
    """Background task: collect energy data every 15 minutes"""
    while True:
        try:
            data = await collector.get_energy_data()
            if data:
                await writer.write_energy_metrics(data)
                latest_data["daily_energy"] = data["daily"]
                logger.info(f"Energy data: Daily={data['daily']}Wh")
        except Exception as e:
            logger.error(f"Error in energy collector: {e}")

        await asyncio.sleep(ENERGY_INTERVAL)


# HTML Dashboard
HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
    <title>fronius2vim Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        :root {
            --evcc-green: #baffcb;
            --evcc-dark-green: #0fde41;
            --evcc-darker-green: #0ba631;
            --evcc-yellow: #faf000;
            --evcc-orange: #ff9000;
            --evcc-red: #fc440f;
            --bs-gray-dark: #28293e;
            --bs-gray-medium: #93949e;
            --bs-gray-bright: #f3f3f7;
            --bs-gray-brighter: #f9f9fb;
            --evcc-background: var(--bs-gray-bright);
            --evcc-card: #ffffff;
            --evcc-box-border: var(--bs-gray-brighter);
            --evcc-text: var(--bs-gray-dark);
            --evcc-text-secondary: var(--bs-gray-medium);
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: var(--evcc-background);
            color: var(--evcc-text);
            min-height: 100vh;
        }
        
        /* Top Bar */
        .top-bar {
            background: var(--evcc-card);
            border-bottom: 1px solid var(--evcc-box-border);
            padding: 16px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .site-title {
            font-size: 1.125rem;
            font-weight: 700;
            letter-spacing: 0.5px;
            color: var(--evcc-text);
            text-transform: uppercase;
        }
        
        .status-badge {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.875rem;
            color: var(--evcc-text-secondary);
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #ef4444;
        }
        
        .status-dot.connected {
            background: var(--evcc-dark-green);
        }
        
        /* Main Container */
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 24px;
        }
        
        /* Metric Cards */
        .metrics {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }
        
        @media (max-width: 640px) {
            .metrics {
                grid-template-columns: 1fr;
            }
        }
        
        .metric-card {
            background: var(--evcc-card);
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        
        .metric-label {
            font-size: 0.75rem;
            color: var(--evcc-text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }
        
        .metric-value {
            font-size: 2.5rem;
            font-weight: 700;
            color: var(--evcc-text);
            line-height: 1;
        }
        
        .metric-unit {
            font-size: 1rem;
            font-weight: 400;
            color: var(--evcc-text-secondary);
            margin-left: 4px;
        }
        
        /* Chart Cards */
        .chart-card {
            background: var(--evcc-card);
            border-radius: 16px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        
        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }
        
        .chart-title {
            font-size: 0.875rem;
            font-weight: 600;
            color: var(--evcc-text);
        }
        
        .timestamp {
            font-size: 0.75rem;
            color: var(--evcc-text-secondary);
        }
        
        /* Legend */
        .legend {
            display: flex;
            gap: 16px;
            margin-bottom: 16px;
            padding: 10px 14px;
            background: #f9fafb;
            border-radius: 8px;
        }
        
        .legend-item {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.75rem;
            color: var(--evcc-text-secondary);
        }
        
        .legend-color {
            width: 12px;
            height: 12px;
            border-radius: 2px;
        }
        
        /* Metrics Log */
        .metrics-log {
            max-height: 300px;
            overflow-y: auto;
            font-family: 'SF Mono', Monaco, Inconsolata, 'Fira Code', monospace;
            font-size: 0.75rem;
            line-height: 1.5;
        }
        
        .log-entry {
            display: flex;
            gap: 12px;
            padding: 8px 12px;
            border-bottom: 1px solid var(--evcc-box-border);
            animation: fadeIn 0.3s ease;
        }
        
        .log-entry:last-child {
            border-bottom: none;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; background: rgba(11, 166, 49, 0.1); }
            to { opacity: 1; background: transparent; }
        }
        
        .log-time {
            color: var(--evcc-text-secondary);
            flex-shrink: 0;
            min-width: 60px;
        }
        
        .log-status {
            flex-shrink: 0;
            width: 16px;
            text-align: center;
        }
        
        .log-status.success {
            color: var(--evcc-dark-green);
        }
        
        .log-status.error {
            color: #ef4444;
        }
        
        .log-data {
            flex: 1;
            word-break: break-all;
        }
        
        .log-metric {
            color: var(--evcc-text);
            font-weight: 600;
        }
        
        .log-value {
            color: #faf000;
        }
        
        .log-labels {
            color: var(--evcc-text-secondary);
        }
        
        .log-empty {
            padding: 24px;
            text-align: center;
            color: var(--evcc-text-secondary);
            font-style: italic;
        }
        
        /* Footer */
        .footer {
            text-align: center;
            padding: 24px;
            color: var(--evcc-text-secondary);
            font-size: 0.75rem;
        }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <div class="top-bar">
        <div class="site-title">fronius2vim</div>
        <div class="status-badge">
            <span class="status-dot" id="statusDot"></span>
            <span id="statusText">Connecting...</span>
        </div>
    </div>
    
    <div class="container">
        <!-- Two Metric Cards -->
        <div class="metrics">
            <div class="metric-card">
                <div class="metric-label">Current Power</div>
                <div class="metric-value">
                    <span id="power">--</span><span class="metric-unit">kW</span>
                </div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Daily Energy</div>
                <div class="metric-value">
                    <span id="dailyEnergy">--</span><span class="metric-unit">kWh</span>
                </div>
            </div>
        </div>
        
        <!-- Today's Chart -->
        <div class="chart-card">
            <div class="chart-header">
                <div class="chart-title">Energy Generation Today</div>
                <span class="timestamp" id="timestamp">--</span>
            </div>
            <div class="legend">
                <div class="legend-item">
                    <div class="legend-color" style="background: rgba(11, 166, 49, 0.8);"></div>
                    <span>Energy (kWh)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #faf000;"></div>
                    <span>Power (kW)</span>
                </div>
            </div>
            <canvas id="combinedChart"></canvas>
        </div>
        
        <!-- 7 Day Chart -->
        <div class="chart-card">
            <div class="chart-header">
                <div class="chart-title">Last 7 Days Production</div>
            </div>
            <canvas id="sevenDayChart"></canvas>
        </div>
        
        <!-- Raw Metrics Log -->
        <div class="chart-card">
            <div class="chart-header">
                <div class="chart-title">Raw Metrics to VictoriaMetrics</div>
                <span class="timestamp">Last 50 writes</span>
            </div>
            <div class="metrics-log" id="metricsLog">
                <div class="log-empty">Waiting for metrics...</div>
            </div>
        </div>
    </div>
    
    <div class="footer">
        fronius2vim
    </div>

    <script>
        let ws = null;
        
        // Chart.js setup for combined chart
        const ctx = document.getElementById('combinedChart').getContext('2d');
        const combinedChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'Energy (kWh)',
                        data: [],
                        backgroundColor: 'rgba(11, 166, 49, 0.8)',
                        borderColor: 'rgba(11, 166, 49, 1)',
                        borderWidth: 0,
                        borderRadius: 3,
                        yAxisID: 'y',
                        order: 2
                    },
                    {
                        label: 'Power (kW)',
                        data: [],
                        type: 'line',
                        borderColor: '#faf000',
                        backgroundColor: 'rgba(250, 240, 0, 0.1)',
                        borderWidth: 2,
                        tension: 0.4,
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        yAxisID: 'y1',
                        order: 1
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                interaction: {
                    mode: 'index',
                    intersect: false,
                },
                scales: {
                    y: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        beginAtZero: true,
                        grid: { 
                            color: 'rgba(0, 0, 0, 0.04)',
                            drawBorder: false
                        },
                        ticks: { 
                            color: '#6b7280',
                            font: { size: 11 }
                        }
                    },
                    y1: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        beginAtZero: true,
                        max: 35,
                        grid: { display: false },
                        ticks: { 
                            color: '#93949e',
                            font: { size: 11 }
                        }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { 
                            color: '#6b7280',
                            font: { size: 11 },
                            maxRotation: 45,
                            autoSkip: true,
                            maxTicksLimit: 12
                        }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(45, 51, 66, 0.95)',
                        padding: 12,
                        cornerRadius: 8,
                        titleFont: { size: 13 },
                        bodyFont: { size: 12 }
                    }
                }
            }
        });

        // Fetch combined data
        async function fetchCombinedData() {
            try {
                const [historyRes, powerRes] = await Promise.all([
                    fetch('/api/history'),
                    fetch('/api/power')
                ]);
                
                const historyData = await historyRes.json();
                const powerData = await powerRes.json();
                
                if (historyData.intervals && historyData.intervals.length > 0) {
                    combinedChart.data.labels = historyData.intervals.map(i => i.time);
                    combinedChart.data.datasets[0].data = historyData.intervals.map(i => i.kwh);
                }
                
                if (powerData.points && powerData.points.length > 0) {
                    combinedChart.data.datasets[1].data = powerData.points.map(p => p.power / 1000);
                }
                
                combinedChart.update();
            } catch (error) {
                console.error('Failed to fetch combined data:', error);
            }
        }

        fetchCombinedData();
        setInterval(fetchCombinedData, 300000);

        // 7-day chart setup
        const sevenDayCtx = document.getElementById('sevenDayChart').getContext('2d');
        const sevenDayChart = new Chart(sevenDayCtx, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [{
                    label: 'Energy (kWh)',
                    data: [],
                    backgroundColor: 'rgba(15, 222, 65, 0.8)',
                    borderColor: 'rgba(15, 222, 65, 1)',
                    borderWidth: 0,
                    borderRadius: 4,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { 
                            color: 'rgba(0, 0, 0, 0.04)',
                            drawBorder: false
                        },
                        ticks: { 
                            color: '#93949e',
                            font: { size: 11 }
                        }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { 
                            color: '#93949e',
                            font: { size: 11 }
                        }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(45, 51, 66, 0.95)',
                        padding: 12,
                        cornerRadius: 8,
                        callbacks: {
                            label: function(context) {
                                return context.parsed.y.toFixed(2) + ' kWh';
                            }
                        }
                    }
                }
            }
        });

        async function fetchSevenDayData() {
            try {
                const response = await fetch('/api/history/7days');
                const data = await response.json();

                if (data.days && data.days.length > 0) {
                    sevenDayChart.data.labels = data.days.map(d => d.date);
                    sevenDayChart.data.datasets[0].data = data.days.map(d => d.kwh);
                    sevenDayChart.update();
                }
            } catch (error) {
                console.error('Failed to fetch 7-day data:', error);
            }
        }

        fetchSevenDayData();
        setInterval(fetchSevenDayData, 3600000);

        function connect() {
            ws = new WebSocket(`ws://${window.location.host}/ws`);
            
            ws.onopen = () => {
                document.getElementById('statusText').textContent = 'Connected';
                document.getElementById('statusDot').className = 'status-dot connected';
            };
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.power !== undefined) {
                    document.getElementById('power').textContent = (data.power / 1000).toFixed(2);
                }
                
                if (data.daily_energy !== undefined) {
                    document.getElementById('dailyEnergy').textContent = (data.daily_energy / 1000).toFixed(2);
                }

                if (data.timestamp) {
                    document.getElementById('timestamp').textContent = data.timestamp;
                }
                
                // Update metrics log display
                if (data.metrics_log && data.metrics_log.length > 0) {
                    const logContainer = document.getElementById('metricsLog');
                    logContainer.innerHTML = data.metrics_log.map(entry => {
                        const labels = Object.entries(entry.labels || {}).map(([k,v]) => `${k}="${v}"`).join(', ');
                        return `
                            <div class="log-entry">
                                <span class="log-time">${entry.timestamp}</span>
                                <span class="log-status ${entry.status}">${entry.status === 'success' ? '✓' : '✗'}</span>
                                <span class="log-data">
                                    <span class="log-metric">${entry.metric}</span>
                                    <span class="log-value">${entry.value}</span>
                                    <span class="log-labels">{${labels}}</span>
                                </span>
                            </div>
                        `;
                    }).join('');
                }
            };
            
            ws.onclose = () => {
                document.getElementById('statusText').textContent = 'Disconnected';
                document.getElementById('statusDot').className = 'status-dot';
                setTimeout(connect, 5000);
            };
            
            ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };
        }
        
        connect();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard HTML"""
    return HTML_DASHBOARD


@app.get("/api/data")
async def get_data():
    """REST API endpoint for current data"""
    return latest_data


@app.get("/api/history")
async def get_history():
    """Query VictoriaMetrics for today's energy data per 15min intervals"""
    try:
        # Calculate today's start timestamp (midnight UTC for VictoriaMetrics)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        start_of_day_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        start_timestamp = int(start_of_day_utc.timestamp())

        # Query VictoriaMetrics for daily energy data today (hourly)
        query_url = f"{VICTORIAMETRICS_URL}/api/v1/query_range"
        params = {
            "query": "fronius_daily_energy_watthours",
            "start": start_timestamp,
            "end": int(now_utc.timestamp()),
            "step": "1h",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(query_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success" and data.get("data", {}).get("result"):
                result = data["data"]["result"][0]
                values = result.get("values", [])

                # Calculate kWh per hour interval
                intervals = []
                for i in range(1, len(values)):
                    timestamp = int(values[i][0])
                    current_wh = float(values[i][1])
                    previous_wh = float(values[i - 1][1])
                    kwh_generated = (current_wh - previous_wh) / 1000

                    # Skip negative values (inverter counter reset at midnight)
                    if kwh_generated >= 0:
                        intervals.append(
                            {
                                "time": datetime.fromtimestamp(timestamp).strftime(
                                    "%H:00"
                                ),
                                "kwh": round(kwh_generated, 2),
                            }
                        )

                return {"intervals": intervals}

        return {"intervals": []}
    except Exception as e:
        logger.error(f"Failed to fetch history: {e}")
        return {"intervals": [], "error": str(e)}


@app.get("/api/power")
async def get_power():
    """Query VictoriaMetrics for today's power data (watts)"""
    try:
        # Calculate today's start timestamp (midnight UTC for VictoriaMetrics)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        start_of_day_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        start_timestamp = int(start_of_day_utc.timestamp())

        # Query VictoriaMetrics for power data today (hourly to match energy)
        query_url = f"{VICTORIAMETRICS_URL}/api/v1/query_range"
        params = {
            "query": "avg_over_time(fronius_power_watts[1h])",
            "start": start_timestamp,
            "end": int(now_utc.timestamp()),
            "step": "1h",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(query_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "success" and data.get("data", {}).get("result"):
                result = data["data"]["result"][0]
                values = result.get("values", [])

                points = []
                for value in values:
                    timestamp = int(value[0])
                    power = float(value[1])
                    points.append(
                        {
                            "time": datetime.fromtimestamp(timestamp).strftime("%H:00"),
                            "power": round(power, 0),
                        }
                    )

                return {"points": points}

        return {"points": []}
    except Exception as e:
        logger.error(f"Failed to fetch power: {e}")
        return {"points": [], "error": str(e)}


@app.get("/api/history/7days")
async def get_7day_history():
    """Query VictoriaMetrics for daily energy production over last 7 days"""
    try:
        now = datetime.now()
        # Build list of last 7 days
        days_list = []
        for i in range(6, -1, -1):
            day_date = now - timedelta(days=i)
            day_start = day_date.replace(hour=0, minute=0, second=0, microsecond=0)
            day_label = day_start.strftime("%a %d")
            days_list.append(
                {
                    "date": day_label,
                    "kwh": 0.0,
                    "start_ts": int(day_start.timestamp()),
                    "end_ts": int((day_start + timedelta(days=1)).timestamp()),
                }
            )

        # Query data at 15m intervals and find daily max (counters reset at midnight)
        query_url = f"{VICTORIAMETRICS_URL}/api/v1/query_range"
        params = {
            "query": "fronius_daily_energy_watthours",
            "start": days_list[0]["start_ts"],
            "end": int(now.timestamp()),
            "step": "15m",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(query_url, params=params)
            response.raise_for_status()
            data = response.json()

            # Collect all values by date
            values_by_date = {}
            if data.get("status") == "success" and data.get("data", {}).get("result"):
                for result in data["data"]["result"]:
                    for value in result.get("values", []):
                        timestamp = int(value[0])
                        wh = float(value[1])
                        kwh = wh / 1000

                        # Find max for each day (date in YYYY-MM-DD format for grouping)
                        day_key = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
                        day_label = datetime.fromtimestamp(timestamp).strftime("%a %d")
                        if (
                            day_key not in values_by_date
                            or kwh > values_by_date[day_key]["kwh"]
                        ):
                            values_by_date[day_key] = {"kwh": kwh, "label": day_label}

                # Fill in the days list with actual values
                for day in days_list:
                    for day_data in values_by_date.values():
                        if day_data["label"] == day["date"]:
                            day["kwh"] = round(day_data["kwh"], 2)
                            break

            # Return clean format without internal fields
            return {
                "days": [{"date": d["date"], "kwh": d["kwh"]} for d in days_list],
            }

    except Exception as e:
        logger.error(f"Failed to fetch 7-day history: {e}")
        return {"days": [], "error": str(e)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates"""
    await websocket.accept()
    try:
        while True:
            data = {
                "power": latest_data.get("power", 0),
                "daily_energy": latest_data.get("daily_energy", 0),
                "timestamp": latest_data.get("timestamp", ""),
                "metrics_log": metrics_log,
            }
            await websocket.send_json(data)
            await asyncio.sleep(REALTIME_INTERVAL)
    except Exception:
        await websocket.close()


@app.get("/api/metrics-log")
async def get_metrics_log():
    """Get recent metrics written to VictoriaMetrics"""
    return {"metrics": metrics_log}


@app.on_event("startup")
async def startup_event():
    """Start background collectors on app startup"""
    collector = FroniusCollector(FRONIUS_HOST)
    writer = VictoriaMetricsWriter(VICTORIAMETRICS_URL)

    logger.info(f"Starting fronius2vim")
    logger.info(f"Fronius host: {FRONIUS_HOST}")
    logger.info(f"VictoriaMetrics URL: {VICTORIAMETRICS_URL}")
    logger.info(f"Realtime interval: {REALTIME_INTERVAL}s")
    logger.info(f"Energy interval: {ENERGY_INTERVAL}s")

    # Start background tasks
    asyncio.create_task(realtime_collector(collector, writer))
    asyncio.create_task(energy_collector(collector, writer))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT)
