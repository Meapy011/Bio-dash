// static/dashboard.js
function updateDashboard() {
    fetch('/api/data')
        .then(response => response.json())
        .then(data => {
            document.getElementById('spo2').innerText = data.spo2;
            document.getElementById('hr').innerText = data.hr;
            document.getElementById('battery').innerText = data.battery;

            const statusEl = document.getElementById('status');
            statusEl.innerText = data.status;

            if (data.status === 'Connected') {
                statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30";
            } else {
                statusEl.className = "px-3 py-1 rounded-full text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30";
            }
        })
        .catch(err => {
            console.error("Error updating dashboard parameters:", err);
        });
}

// Poll the local API endpoint every 1000ms (1 second)
setInterval(updateDashboard, 1000);
updateDashboard();
