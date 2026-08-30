var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
if (tg) {
    tg.ready();
    tg.expand();
}

var chartInstance = null;

function logDebug(msg) {
    var el = document.getElementById('debugLog');
    if (el) {
        el.innerHTML += '<div>' + msg + '</div>';
    }
}

function getUserId() {
    var urlParams = new URLSearchParams(window.location.search);
    var uidFromUrl = urlParams.get('user_id');
    if (uidFromUrl && uidFromUrl !== '0') {
        return uidFromUrl;
    }

    if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) {
        return tg.initDataUnsafe.user.id;
    }

    return 0;
}

async function loadData(manual) {
    var btn = document.getElementById('refreshBtn');
    if (manual && btn) {
        btn.innerText = '⏳...';
    }

    var logEl = document.getElementById('debugLog');
    if (logEl) logEl.innerHTML = '';

    var userId = getUserId();
    var initDataRaw = (tg && tg.initData) ? tg.initData : '';

    logDebug('🔍 Rilevato User ID: ' + userId);
    logDebug('📦 URL Search: ' + (window.location.search || 'Nessun parametro'));

    try {
        var fetchUrl = '/api/data?user_id=' + userId + '&init_data=' + encodeURIComponent(initDataRaw) + '&_t=' + Date.now();
        logDebug('🌐 Chiamata a: ' + fetchUrl);

        var res = await fetch(fetchUrl);
        var data = await res.json();

        logDebug('✅ Risposta API: ' + JSON.stringify(data));

        if (data.error) {
            logDebug('❌ Errore Backend: ' + data.error);
        }

        document.getElementById('currentMonth').innerText = data.month || '--';
        document.getElementById('totalIncome').innerText = '+€' + (data.income || 0).toFixed(2);
        document.getElementById('totalExpense').innerText = '-€' + (data.expense || 0).toFixed(2);

        var balance = data.balance || 0;
        var balanceEl = document.getElementById('netBalance');
        balanceEl.innerText = (balance >= 0 ? '+' : '') + '€' + balance.toFixed(2);
        balanceEl.className = 'text-sm font-bold ' + (balance >= 0 ? 'text-emerald-400' : 'text-rose-400');

        // Render Grafico
        var ctx = document.getElementById('expenseChart').getContext('2d');
        var noData = document.getElementById('noDataText');

        if (!data.categories || data.categories.length === 0) {
            noData.classList.remove('hidden');
            if (chartInstance) {
                chartInstance.destroy();
                chartInstance = null;
            }
        } else {
            noData.classList.add('hidden');
            if (chartInstance) {
                chartInstance.destroy();
            }

            chartInstance = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: data.categories.map(function(c) { return c.name; }),
                    datasets: [{
                        data: data.categories.map(function(c) { return c.value; }),
                        backgroundColor: [
                            '#f43f5e', '#3b82f6', '#10b981', '#f59e0b',
                            '#8b5cf6', '#06b6d4', '#ec4899', '#14b8a6'
                        ]
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { color: '#cbd5e1', font: { size: 11 } }
                        }
                    }
                }
            });
        }

        // Render Saldi Amici
        var fContainer = document.getElementById('friendsBalances');
        if (!data.friends || data.friends.length === 0) {
            fContainer.innerHTML = '<p class="text-slate-500">Nessun debito o credito aperto.</p>';
        } else {
            fContainer.innerHTML = '';
            data.friends.forEach(function(f) {
                var isCredit = f.balance > 0;
                var color = isCredit ? 'text-emerald-400' : (f.balance < 0 ? 'text-rose-400' : 'text-slate-400');
                var desc = isCredit ? 'ti deve' : (f.balance < 0 ? 'devi dare' : 'in pari');
                fContainer.innerHTML += '<div class="flex justify-between items-center py-1.5 border-b border-slate-700/50">' +
                    '<span class="font-medium text-slate-200">' + f.name + '</span>' +
                    '<span class="' + color + ' font-semibold">' + desc + ' €' + Math.abs(f.balance).toFixed(2) + '</span>' +
                    '</div>';
            });
        }
    } catch (err) {
        logDebug('❌ Errore Fetch JS: ' + err.message);
        console.error("Errore fetch dashboard:", err);
    } finally {
        if (manual && btn) {
            btn.innerText = '🔄 Aggiorna';
        }
    }
}

document.addEventListener('DOMContentLoaded', function() {
    loadData(false);
    window.addEventListener('focus', function() { loadData(false); });
});