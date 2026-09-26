// Common configuration options for fluid charting steps
const chartOptions = (titleColor, gridColor) => ({
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    scales: {
        x: { type: 'realtime', realtime: { duration: 30000, refresh: 100, delay: 500 } },
        y: { grid: { color: '#2d2d2d' }, ticks: { color: '#a3a3a3' } }
    },
    plugins: { legend: { display: false } }
});

// Initialize Oxygen Graph
const ctxSpO2 = document.getElementById('spo2Chart').getContext('2d');
const spo2Chart = new Chart(ctxSpO2, {
    type: 'line',
    data: { datasets: [{ label: 'SpO2', borderColor: '#38bdf8', borderWidth: 3, pointRadius: 0, data: [] }] },
    options: {
        ...chartOptions(),
        scales: { ...chartOptions().scales, y: { min: 80, max: 100, grid: { color: '#2d2d2d' }, ticks: { color: '#a3a3a3' } } }
    }
});

// Initialize Heart Rate Graph
const ctxHR = document.getElementById('hrChart').getContext('2d');
const hrChart = new Chart(ctxHR, {
    type: 'line',
    data: { datasets: [{ label: 'Heart Rate', borderColor: '#f43f5e', borderWidth: 3, pointRadius: 0, data: [] }] },
    options: {
        ...chartOptions(),
        scales: { ...chartOptions().scales, y: { min: 40, max: 120, grid: { color: '#2d2d2d' }, ticks: { color: '#a3a3a3' } } }
    }
});

const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Handle incoming data packets inside the real-time SSE pipe
const eventSource = new EventSource("/api/vitals-stream");
eventSource.onmessage = function(event) {
    const data = JSON.parse(event.data);
    const now = Date.now();
    
    document.getElementById('status-val').innerText = 'Device Status: ' + data.status;
    
    if (data.status === "Connected" && data.spo2 > 0) {
        document.getElementById('spo2-val').innerText = data.spo2 + '%';
        document.getElementById('hr-val').innerText = data.hr + ' BPM';
        
        // Push points directly into the active chart buffers instantly
        spo2Chart.data.datasets[0].data.push({ x: now, y: data.spo2 });
        hrChart.data.datasets[0].data.push({ x: now, y: data.hr });
    } else {
        document.getElementById('spo2-val').innerText = '--%';
        document.getElementById('hr-val').innerText = '-- BPM';
    }
    
    if (data.status === "Scanning") {
        document.getElementById('selector-panel').style.display = 'block';
    } else {
        document.getElementById('selector-panel').style.display = 'none';
    }
};

async function updateDeviceList() {
    try {
        const response = await fetch('/api/scan-results');
        const devices = await response.json();
        const container = document.getElementById('device-list');
        container.innerHTML = '';
        if (devices.length === 0) {
            container.innerHTML = '<div style="text-align:center;color:#737373;padding:0.5rem;">Searching for wrist devices...</div>';
            return;
        }
        devices.forEach(d => {
            const div = document.createElement('div');
            div.className = 'device-item';
            div.innerHTML = `
                <div><strong>${esc(d.name)}</strong><br><span style="font-size:0.75rem;color:#a3a3a3;">${esc(d.address)} · ${esc(d.rssi)} dBm</span></div>
                <button class="btn" data-mac="${esc(d.address)}">Connect</button>
            `;
            container.appendChild(div);
        });
    } catch (err) {}
}

async function connectDevice(mac) {
    await fetch('/api/connect', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({address: mac})
    });
}

document.getElementById('device-list').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mac]');
    if (b) connectDevice(b.dataset.mac);
});

setInterval(updateDeviceList, 3000);
