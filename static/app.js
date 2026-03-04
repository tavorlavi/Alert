// ============================================
// Alert Dashboard - Main Application Script
// State Machine: IDLE → ALERT → COUNTDOWN → EXPIRED
// ============================================

(function () {
    'use strict';

    // --- State Machine ---
    const STATE = {
        IDLE: 'idle',
        ALERT: 'alert',
        COUNTDOWN: 'countdown',
        EXPIRED: 'expired'
    };

    // --- DOM Elements ---
    const $ = id => document.getElementById(id);

    // Navbar
    const statusDot = $('statusDot');
    const statusText = $('statusText');

    // Timer
    const timerCard = $('timerCard');
    const timerBadge = $('timerBadge');
    const timerBadgeIcon = $('timerBadgeIcon');
    const timerBadgeText = $('timerBadgeText');
    const timerTimestamp = $('timerTimestamp');
    const timerMessage = $('timerMessage');
    const countdownSection = $('countdownSection');
    const countHours = $('countHours');
    const countMinutes = $('countMinutes');
    const countSeconds = $('countSeconds');
    const progressBar = $('progressBar');
    const timerForecast = $('timerForecast');
    const forecastText = $('forecastText');

    // Toast & Particles
    const toastContainer = $('toastContainer');
    const particlesContainer = $('particles');

    // Stats
    const statForecasts = $('statForecasts');
    const statAlertRounds = $('statAlertRounds');
    const statMatched = $('statMatched');
    const statAvgDiff = $('statAvgDiff');
    const comparisonBody = $('comparisonBody');
    const refreshStatsBtn = $('refreshStatsBtn');

    // --- Application State ---
    let appState = STATE.IDLE;
    let currentTargetTime = null;
    let countdownInterval = null;
    let countdownStartTime = null;
    let ws = null;
    let reconnectAttempts = 0;
    let maxReconnectAttempts = 50;
    let isFirstMessage = true;
    let hasActiveAlert = false;
    let lastTelegramData = null;

    // ============================================
    // Particles
    // ============================================
    function createParticles() {
        for (let i = 0; i < 30; i++) {
            const p = document.createElement('div');
            p.className = 'particle';
            const size = Math.random() * 4 + 2;
            p.style.width = size + 'px';
            p.style.height = size + 'px';
            p.style.left = Math.random() * 100 + '%';
            p.style.animationDuration = (Math.random() * 20 + 15) + 's';
            p.style.animationDelay = (Math.random() * 20) + 's';
            particlesContainer.appendChild(p);
        }
    }

    // ============================================
    // WebSocket Connection
    // ============================================
    function connectWebSocket() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${location.host}/ws`);

        ws.onopen = () => {
            reconnectAttempts = 0;
            statusDot.className = 'status-dot connected';
            statusText.textContent = 'מחובר';
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                handleMessage(data);
            } catch (e) {
                console.warn('Bad WS message:', e);
            }
        };

        ws.onclose = () => {
            statusDot.className = 'status-dot error';
            statusText.textContent = 'מנותק';
            if (reconnectAttempts < maxReconnectAttempts) {
                const delay = Math.min(1000 * Math.pow(1.5, reconnectAttempts), 30000);
                reconnectAttempts++;
                setTimeout(connectWebSocket, delay);
            }
        };

        ws.onerror = () => {
            statusDot.className = 'status-dot error';
            statusText.textContent = 'שגיאה';
        };
    }

    // ============================================
    // Message Router
    // ============================================
    function handleMessage(data) {
        switch (data.msg_type) {
            case 'init':
                handleAlertState(data.oref_alerts || []);
                // Store telegram data but only show if alert is active
                if (data.telegram && data.telegram.has_data) {
                    lastTelegramData = data.telegram;
                    if (hasActiveAlert) {
                        applyForecastData(data.telegram);
                    }
                }
                isFirstMessage = false;
                break;

            case 'telegram_timing':
                lastTelegramData = data;
                if (hasActiveAlert) {
                    applyForecastData(data);
                    if (!isFirstMessage) {
                        showToast('⏱️ צפי חדש התקבל!');
                    }
                }
                break;

            case 'oref_alert':
                handleNewAlert(data.alert, data.is_new);
                break;

            case 'oref_clear':
                handleAlertClear();
                break;

            default:
                if (data.has_data !== undefined) {
                    lastTelegramData = data;
                    if (hasActiveAlert) applyForecastData(data);
                }
                break;
        }
    }

    // ============================================
    // Alert Handlers (Oref alerts)
    // ============================================
    function handleAlertState(alerts) {
        if (!alerts || alerts.length === 0) {
            if (appState === STATE.ALERT) {
                handleAlertClear();
            }
            return;
        }
        // There are active alerts
        const combined = {
            title: alerts[0]?.title || 'התרעה',
            cities: alerts.map(a => a.data).flat().filter(Boolean),
            desc: alerts[0]?.desc || '',
            category_info: alerts[0]?.category_info || {},
            timestamp: alerts[0]?.alertDate
        };
        showAlert(combined);
    }

    function handleNewAlert(alert, isNew) {
        showAlert(alert);

        if (isNew && !isFirstMessage) {
            const cities = alert.cities ? alert.cities.join(', ') : '';
            showToast(`🚨 ${alert.title}: ${cities}`);
            playAlertSound();
        }

        // If we have stored telegram data, now show it
        if (lastTelegramData && lastTelegramData.has_data) {
            applyForecastData(lastTelegramData);
        }
    }

    function showAlert(alert) {
        hasActiveAlert = true;

        // Update timer card to ALERT state (waiting for forecast)
        if (appState === STATE.IDLE) {
            setAppState(STATE.ALERT);
        }
    }

    function handleAlertClear() {
        hasActiveAlert = false;

        // If not counting down, go back to idle
        if (appState === STATE.ALERT) {
            setAppState(STATE.IDLE);
        }
    }

    // ============================================
    // Forecast / Countdown
    // ============================================
    function applyForecastData(data) {
        if (!data || !data.has_data) return;

        if (data.text) {
            forecastText.textContent = data.text;
            timerForecast.style.display = '';
        }

        if (data.target_time) {
            currentTargetTime = new Date(data.target_time);
            countdownStartTime = new Date();
            timerTimestamp.textContent = formatTime(currentTargetTime);
        }

        setAppState(STATE.COUNTDOWN);
    }

    // ============================================
    // Countdown Timer
    // ============================================
    function startCountdown() {
        clearCountdownInterval();
        updateCountdown();
        countdownInterval = setInterval(updateCountdown, 1000);
    }

    function clearCountdownInterval() {
        if (countdownInterval) {
            clearInterval(countdownInterval);
            countdownInterval = null;
        }
    }

    function updateCountdown() {
        if (!currentTargetTime) return;
        const now = new Date();
        const diff = currentTargetTime - now;

        if (diff <= 0) {
            setAppState(STATE.EXPIRED);
            return;
        }

        const totalSeconds = Math.floor(diff / 1000);
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;

        countHours.textContent = String(hours).padStart(2, '0');
        countMinutes.textContent = String(minutes).padStart(2, '0');
        countSeconds.textContent = String(seconds).padStart(2, '0');

        // Progress bar
        if (countdownStartTime) {
            const totalDuration = currentTargetTime - countdownStartTime;
            const elapsed = now - countdownStartTime;
            const pct = Math.max(0, Math.min(100, ((totalDuration - elapsed) / totalDuration) * 100));
            progressBar.style.width = pct + '%';

            if (pct < 15) {
                progressBar.className = 'progress-bar critical';
            } else if (pct < 40) {
                progressBar.className = 'progress-bar warning';
            } else {
                progressBar.className = 'progress-bar';
            }

            // Urgent styling when < 60 seconds
            if (totalSeconds < 60) {
                countHours.classList.add('urgent');
                countMinutes.classList.add('urgent');
                countSeconds.classList.add('urgent');
            } else {
                countHours.classList.remove('urgent');
                countMinutes.classList.remove('urgent');
                countSeconds.classList.remove('urgent');
            }
        }
    }

    // ============================================
    // State Machine
    // ============================================
    function setAppState(newState) {
        appState = newState;
        timerCard.className = 'timer-card';

        switch (newState) {
            case STATE.IDLE:
                timerCard.classList.add('state-idle');
                timerBadge.className = 'timer-badge';
                timerBadgeIcon.textContent = '✅';
                timerBadgeText.textContent = 'הכל שקט';
                timerMessage.textContent = 'אין התרעות פעילות כרגע';
                timerTimestamp.textContent = '';
                countdownSection.style.display = 'none';
                timerForecast.style.display = 'none';
                clearCountdownInterval();
                currentTargetTime = null;
                countdownStartTime = null;
                progressBar.style.width = '100%';
                progressBar.className = 'progress-bar';
                countHours.className = 'countdown-value';
                countMinutes.className = 'countdown-value';
                countSeconds.className = 'countdown-value';
                break;

            case STATE.ALERT:
                timerCard.classList.add('state-alert');
                timerBadge.className = 'timer-badge alert';
                timerBadgeIcon.textContent = '🚨';
                timerBadgeText.textContent = 'התרעה פעילה';
                timerMessage.textContent = 'ממתין לצפי...';
                countdownSection.style.display = 'none';
                break;

            case STATE.COUNTDOWN:
                timerCard.classList.add('state-countdown');
                timerBadge.className = 'timer-badge countdown';
                timerBadgeIcon.textContent = '⏱️';
                timerBadgeText.textContent = 'ספירה לאחור';
                timerMessage.textContent = 'צפי התקבל — ספירה לאחור';
                countdownSection.style.display = '';
                startCountdown();
                break;

            case STATE.EXPIRED:
                clearCountdownInterval();
                timerCard.classList.add('state-expired');
                timerBadge.className = 'timer-badge expired';
                timerBadgeIcon.textContent = '⌛';
                timerBadgeText.textContent = 'הזמן עבר';
                timerMessage.textContent = 'הצפי חלף';
                countHours.textContent = '00';
                countMinutes.textContent = '00';
                countSeconds.textContent = '00';
                progressBar.style.width = '0%';
                progressBar.className = 'progress-bar';
                countHours.className = 'countdown-value';
                countMinutes.className = 'countdown-value';
                countSeconds.className = 'countdown-value';
                break;
        }
    }

    // ============================================
    // Stats Dashboard
    // ============================================
    async function loadStats() {
        try {
            const statsResp = await fetch('/api/stats');

            if (statsResp.ok) {
                const stats = await statsResp.json();
                renderStats(stats);
            }
        } catch (e) {
            console.warn('Could not load stats:', e);
        }
    }

    function renderStats(stats) {
        if (!stats || !stats.summary) return;

        const s = stats.summary;
        statForecasts.textContent = s.total_forecasts ?? '—';
        statAlertRounds.textContent = s.total_alert_rounds ?? '—';
        statMatched.textContent = s.matched ?? '—';
        statAvgDiff.textContent = s.avg_diff_minutes != null ? `${s.avg_diff_minutes} דק׳` : '—';

        // Comparison table
        const comparisons = stats.comparisons || [];
        if (comparisons.length === 0) {
            comparisonBody.innerHTML = '<tr><td colspan="3" class="table-empty">אין נתונים להשוואה</td></tr>';
        } else {
            comparisonBody.innerHTML = comparisons.map(c => {
                return `<tr>
                    <td>${escapeHtml(c.forecast_time || '—')}</td>
                    <td>${c.matched ? escapeHtml(c.real_time || '') : '—'}</td>
                    <td>${getDiffBadge(c)}</td>
                </tr>`;
            }).join('');
        }
    }

    function getDiffBadge(comparison) {
        if (!comparison.matched) {
            // Check if it's "no data" vs "no match"
            const label = comparison.diff_label || 'ללא התאמה';
            if (label === 'אין מידע') {
                return `<span class="diff-badge no-data">${escapeHtml(label)}</span>`;
            }
            return `<span class="diff-badge no-match">${escapeHtml(label)}</span>`;
        }

        const absDiff = Math.abs(comparison.diff_minutes || 0);
        let cls;
        if (absDiff < 2) cls = 'exact';
        else if (absDiff < 10) cls = 'close';
        else if (absDiff < 30) cls = 'moderate';
        else cls = 'far';

        return `<span class="diff-badge ${cls}">${escapeHtml(comparison.diff_label || '')}</span>`;
    }

    // Refresh button
    refreshStatsBtn.addEventListener('click', () => {
        refreshStatsBtn.style.transform = 'rotate(360deg)';
        setTimeout(() => { refreshStatsBtn.style.transform = ''; }, 500);
        loadStats();
    });

    // ============================================
    // Toast & Sound
    // ============================================
    function showToast(message) {
        const toast = document.createElement('div');
        toast.className = 'toast';
        toast.innerHTML = `<span class="toast-icon">🔔</span><span>${message}</span>`;
        toastContainer.appendChild(toast);
        setTimeout(() => { if (toast.parentNode) toast.remove(); }, 4500);
    }

    function playAlertSound() {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.frequency.setValueAtTime(880, ctx.currentTime);
            osc.frequency.setValueAtTime(660, ctx.currentTime + 0.15);
            osc.frequency.setValueAtTime(880, ctx.currentTime + 0.3);
            gain.gain.setValueAtTime(0.15, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.6);
            osc.start(ctx.currentTime);
            osc.stop(ctx.currentTime + 0.6);
        } catch (e) { /* no audio support */ }
    }

    // ============================================
    // Utilities
    // ============================================
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function formatTime(date) {
        return date.toLocaleTimeString('he-IL', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    }

    function formatDateTime(date) {
        return date.toLocaleString('he-IL', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        });
    }

    // ============================================
    // Periodic Refresh
    // ============================================
    function startPolling() {
        setInterval(loadStats, 30000);  // Refresh stats every 30s
    }

    // ============================================
    // Initialize
    // ============================================
    function init() {
        createParticles();
        setAppState(STATE.IDLE);
        loadStats();
        connectWebSocket();
        startPolling();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
