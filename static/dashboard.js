var tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
if (tg) {
    tg.ready();
    tg.expand();
}

var chartInstance = null;
var trendChartInstance = null;

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

    var userId = getUserId();
    var initDataRaw = (tg && tg.initData) ? tg.initData : '';

    try {
        var fetchUrl = '/api/data?user_id=' + userId + '&init_data=' + encodeURIComponent(initDataRaw) + '&_t=' + Date.now();
        var res = await fetch(fetchUrl);
        var data = await res.json();

        document.getElementById('currentMonth').innerText = data.month || '--';
        document.getElementById('totalIncome').innerText = '+€' + (data.income || 0).toFixed(2);
        document.getElementById('totalExpense').innerText = '-€' + (data.expense || 0).toFixed(2);

        var balance = data.balance || 0;
        var balanceEl = document.getElementById('netBalance');
        balanceEl.innerText = (balance >= 0 ? '+' : '') + '€' + balance.toFixed(2);
        balanceEl.className = 'text-sm font-bold ' + (balance >= 0 ? 'text-emerald-400' : 'text-rose-400');

        // 1. RENDER GRAFICO ANDAMENTO STORICO 26 MESI (LINE CHART)
        var trendCtx = document.getElementById('trendChart').getContext('2d');
        var noTrendData = document.getElementById('noTrendDataText');

        if (!data.history || data.history.length === 0) {
            noTrendData.classList.remove('hidden');
            if (trendChartInstance) {
                trendChartInstance.destroy();
                trendChartInstance = null;
            }
        } else {
            noTrendData.classList.add('hidden');
            if (trendChartInstance) {
                trendChartInstance.destroy();
            }

            var labels = data.history.map(function(h) {
                // Formatta "2026-08" in "08/26"
                var parts = h.month.split('-');
                return parts.length === 2 ? parts[1] + '/' + parts[0].slice(2) : h.month;
            });
            var values = data.history.map(function(h) { return h.net; });

            // Colori dinamici dei punti (Verde per attivo, Rosso per disavanzo)
            var pointColors = values.map(function(v) { return v >= 0 ? '#10b981' : '#f43f5e'; });

            trendChartInstance = new Chart(trendCtx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Bilancio Netto (€)',
                        data: values,
                        borderColor: '#6366f1',
                        borderWidth: 2.5,
                        backgroundColor: 'rgba(99, 102, 241, 0.15)',
                        fill: true,
                        tension: 0.35,
                        pointBackgroundColor: pointColors,
                        pointBorderColor: '#0f172a',
                        pointBorderWidth: 2,
                        pointRadius: 4,
                        pointHoverRadius: 6
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: {
                            grid: { color: '#334155' },
                            ticks: { color: '#94a3b8', font: { size: 10 } }
                        },
                        y: {
                            grid: { color: '#334155' },
                            ticks: {
                                color: '#94a3b8',
                                font: { size: 10 },
                                callback: function(val) { return '€' + val; }
                            }
                        }
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    var val = context.parsed.y;
                                    return (val >= 0 ? 'Guadagno: +€' : 'Perdita: -€') + Math.abs(val).toFixed(2);
                                }
                            }
                        }
                    }
                }
            });
        }

        // 2. RENDER GRAFICO CATEGORIE (DOUGHNUT CHART)
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

        // 3. RENDER SALDI AMICI
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
        console.error("Errore caricamento dati dashboard:", err);
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