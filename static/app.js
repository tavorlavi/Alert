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
        EXPIRED: 'expired',
        EARLY: 'early',     // Alert arrived before timer reached zero
        LATE: 'late'        // Alert arrived after timer expired
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

    // Alert details
    const alertDetails = $('alertDetails');
    const alertDetailsIcon = $('alertDetailsIcon');
    const alertDetailsTitle = $('alertDetailsTitle');
    const alertDetailsCities = $('alertDetailsCities');

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
                // Handle active alerts (from persistent alert system — includes both Oref and Telegram)
                handleAlertState(data.oref_alerts || []);
                // Always store and apply forecast data (even in IDLE)
                if (data.telegram && data.telegram.has_data) {
                    lastTelegramData = data.telegram;
                    applyForecastData(data.telegram);
                }
                isFirstMessage = false;
                break;

            case 'telegram_timing':
                lastTelegramData = data;
                // Always apply forecast (show expected time regardless of alert state)
                applyForecastData(data);
                if (!isFirstMessage) {
                    showToast('⏱️ צפי חדש התקבל!');
                }
                break;

            case 'oref_alert':
                handleNewAlert(data.alert, data.is_new);
                break;

            case 'forecast_matched':
                handleForecastMatched(data);
                break;

            case 'oref_clear':
                handleAlertClear();
                break;

            default:
                if (data.has_data !== undefined) {
                    lastTelegramData = data;
                    applyForecastData(data);
                }
                break;
        }
    }

    // ============================================
    // Alert Handlers (Oref alerts)
    // ============================================
    function handleAlertState(alerts) {
        if (!alerts || alerts.length === 0) {
            if (hasActiveAlert) {
                handleAlertClear();
            }
            return;
        }
        // There are active alerts — combine them for display
        // Group by category for display
        const allCities = [];
        let primaryTitle = '';
        let primaryIcon = '🚨';
        let primaryCategory = null;
        
        for (const a of alerts) {
            const cities = a.cities || (a.data ? [].concat(a.data).filter(Boolean) : []);
            allCities.push(...cities);
            if (!primaryTitle) {
                primaryTitle = a.title || a.category_info?.label || 'התרעה';
                primaryIcon = a.category_info?.icon || '🚨';
                primaryCategory = a.category;
            }
        }
        
        const combined = {
            title: primaryTitle,
            cities: [...new Set(allCities)],
            desc: alerts[0]?.desc || '',
            category: primaryCategory,
            category_info: alerts[0]?.category_info || {},
            timestamp: alerts[0]?.timestamp || alerts[0]?.alertDate
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

        // If countdown is running and a missile alert arrives, check timing
        if (alert.category === 1 && (appState === STATE.COUNTDOWN || appState === STATE.EXPIRED) && currentTargetTime) {
            const now = new Date();
            const diffMs = now - currentTargetTime;
            const diffSec = diffMs / 1000;
            const diffMin = Math.abs(diffSec / 60);
            
            if (diffMin < 10) {
                if (diffMs < 0) {
                    setAppState(STATE.EARLY, diffSec);
                } else if (appState === STATE.EXPIRED) {
                    setAppState(STATE.LATE, diffSec);
                }
            }
        }
    }

    function handleForecastMatched(data) {
        if (data.early) {
            setAppState(STATE.EARLY, data.diff_seconds);
        } else if (data.late) {
            setAppState(STATE.LATE, data.diff_seconds);
        }
    }

    function showAlert(alert) {
        hasActiveAlert = true;

        // Display alert details in the panel
        const icon = alert.category_info?.icon || '🚨';
        const title = alert.title || 'התרעה פעילה';
        const cities = alert.cities || [];
        
        alertDetailsIcon.textContent = icon;
        alertDetailsTitle.textContent = title;
        
        if (cities.length > 0) {
            const cityTags = cities.slice(0, 30).map(c => 
                `<span class="city-tag">${escapeHtml(c)}</span>`
            ).join('');
            const extra = cities.length > 30 ? `<span class="city-tag more">+${cities.length - 30} נוספים</span>` : '';
            alertDetailsCities.innerHTML = cityTags + extra;
        } else {
            alertDetailsCities.innerHTML = '';
        }
        alertDetails.style.display = '';

        // Update timer card to ALERT state if not already counting down
        if (appState === STATE.IDLE) {
            setAppState(STATE.ALERT);
        }
    }

    function handleAlertClear() {
        hasActiveAlert = false;
        alertDetails.style.display = 'none';

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

        if (data.target_time) {
            const targetDt = new Date(data.target_time);
            const now = new Date();
            const pastMs = now - targetDt;
            
            // Don't show forecasts that are more than 15 minutes past their target time
            if (pastMs > 15 * 60 * 1000) {
                return;
            }
            
            // Only update countdown target if it's a new/different time
            if (!currentTargetTime || currentTargetTime.getTime() !== targetDt.getTime()) {
                currentTargetTime = targetDt;
                countdownStartTime = new Date();
            }
            timerTimestamp.textContent = 'צפי: ' + formatTime(targetDt);
        }

        if (data.text) {
            forecastText.textContent = data.text;
            timerForecast.style.display = '';
        }

        // If there's an active alert → start countdown
        // If IDLE → just show forecast info without full countdown state
        if (hasActiveAlert && appState !== STATE.COUNTDOWN && appState !== STATE.EXPIRED && appState !== STATE.EARLY && appState !== STATE.LATE) {
            setAppState(STATE.COUNTDOWN);
        } else if (appState === STATE.IDLE && data.target_time) {
            // Show forecast section in idle mode (no countdown animation)
            countdownSection.style.display = '';
            updateCountdownDisplay();
        }
    }

    function updateCountdownDisplay() {
        if (!currentTargetTime) return;
        const now = new Date();
        const diff = currentTargetTime - now;

        if (diff <= 0) {
            countHours.textContent = '00';
            countMinutes.textContent = '00';
            countSeconds.textContent = '00';
            progressBar.style.width = '0%';
            return;
        }

        const totalSeconds = Math.floor(diff / 1000);
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;

        countHours.textContent = String(hours).padStart(2, '0');
        countMinutes.textContent = String(minutes).padStart(2, '0');
        countSeconds.textContent = String(seconds).padStart(2, '0');
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
    function setAppState(newState, diffSeconds) {
        appState = newState;
        timerCard.className = 'timer-card';

        switch (newState) {
            case STATE.IDLE:
                timerCard.classList.add('state-idle');
                timerBadge.className = 'timer-badge';
                timerBadgeIcon.textContent = '✅';
                timerBadgeText.textContent = 'הכל שקט';
                timerMessage.textContent = 'אין התרעות פעילות כרגע';
                alertDetails.style.display = 'none';
                clearCountdownInterval();
                progressBar.style.width = '100%';
                progressBar.className = 'progress-bar';
                countHours.className = 'countdown-value';
                countMinutes.className = 'countdown-value';
                countSeconds.className = 'countdown-value';
                // Keep forecast visible if we have RECENT data
                if (lastTelegramData && lastTelegramData.has_data && lastTelegramData.target_time) {
                    const targetDt = new Date(lastTelegramData.target_time);
                    const pastMs = new Date() - targetDt;
                    if (pastMs <= 15 * 60 * 1000) {
                        // Forecast is still relevant (< 15 min past)
                        timerForecast.style.display = '';
                        timerTimestamp.textContent = 'צפי: ' + formatTime(targetDt);
                        countdownSection.style.display = '';
                        updateCountdownDisplay();
                    } else {
                        // Forecast expired — clear it
                        timerTimestamp.textContent = '';
                        countdownSection.style.display = 'none';
                        timerForecast.style.display = 'none';
                        currentTargetTime = null;
                        countdownStartTime = null;
                        lastTelegramData = null;
                    }
                } else {
                    timerTimestamp.textContent = '';
                    countdownSection.style.display = 'none';
                    timerForecast.style.display = 'none';
                    currentTargetTime = null;
                    countdownStartTime = null;
                }
                break;

            case STATE.ALERT:
                timerCard.classList.add('state-alert');
                timerBadge.className = 'timer-badge alert';
                timerBadgeIcon.textContent = '🚨';
                timerBadgeText.textContent = 'התרעה פעילה';
                if (lastTelegramData && lastTelegramData.has_data && lastTelegramData.target_time) {
                    timerMessage.textContent = 'התרעה פעילה — צפי זמין';
                } else {
                    timerMessage.textContent = 'התרעה פעילה — ממתין לצפי...';
                }
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
                timerMessage.textContent = 'הצפי חלף — ממתין להתרעה...';
                countHours.textContent = '00';
                countMinutes.textContent = '00';
                countSeconds.textContent = '00';
                progressBar.style.width = '0%';
                progressBar.className = 'progress-bar';
                countHours.className = 'countdown-value';
                countMinutes.className = 'countdown-value';
                countSeconds.className = 'countdown-value';
                break;

            case STATE.EARLY: {
                clearCountdownInterval();
                timerCard.classList.add('state-early');
                timerBadge.className = 'timer-badge early';
                timerBadgeIcon.textContent = '⚡';
                timerBadgeText.textContent = 'הגיע מוקדם!';
                const earlyLabel = formatDiffLabel(diffSeconds);
                timerMessage.textContent = `ההתרעה הגיעה מוקדם ב-${earlyLabel}`;
                // Show remaining time as 00:00:00
                countHours.textContent = '00';
                countMinutes.textContent = '00';
                countSeconds.textContent = '00';
                progressBar.style.width = '100%';
                progressBar.className = 'progress-bar early-bar';
                countHours.className = 'countdown-value';
                countMinutes.className = 'countdown-value';
                countSeconds.className = 'countdown-value';
                showToast(`⚡ ההתרעה הגיעה מוקדם ב-${earlyLabel}`);
                break;
            }

            case STATE.LATE: {
                clearCountdownInterval();
                timerCard.classList.add('state-late');
                timerBadge.className = 'timer-badge late';
                timerBadgeIcon.textContent = '⏰';
                timerBadgeText.textContent = 'הגיע באיחור';
                const lateLabel = formatDiffLabel(diffSeconds);
                timerMessage.textContent = `ההתרעה איחרה ב-${lateLabel}`;
                countHours.textContent = '00';
                countMinutes.textContent = '00';
                countSeconds.textContent = '00';
                progressBar.style.width = '0%';
                progressBar.className = 'progress-bar late-bar';
                countHours.className = 'countdown-value';
                countMinutes.className = 'countdown-value';
                countSeconds.className = 'countdown-value';
                showToast(`⏰ ההתרעה איחרה ב-${lateLabel}`);
                break;
            }
        }
    }

    function formatDiffLabel(seconds) {
        const absSec = Math.abs(seconds);
        if (absSec < 60) return `${Math.round(absSec)} שניות`;
        const mins = Math.floor(absSec / 60);
        const secs = Math.round(absSec % 60);
        if (mins < 60) return secs > 0 ? `${mins} דקות ו-${secs} שניות` : `${mins} דקות`;
        const hours = Math.floor(mins / 60);
        const remainMins = mins % 60;
        return remainMins > 0 ? `${hours} שעות ו-${remainMins} דקות` : `${hours} שעות`;
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
        // Update idle display every second + auto-expire stale forecasts
        setInterval(() => {
            if (currentTargetTime) {
                const pastMs = new Date() - currentTargetTime;
                // Auto-expire: if forecast target passed by more than 15 min, clear it
                if (pastMs > 15 * 60 * 1000 && (appState === STATE.IDLE || appState === STATE.EXPIRED)) {
                    currentTargetTime = null;
                    countdownStartTime = null;
                    lastTelegramData = null;
                    if (appState === STATE.EXPIRED) {
                        setAppState(STATE.IDLE);
                    } else {
                        // Just hide forecast sections in IDLE
                        countdownSection.style.display = 'none';
                        timerForecast.style.display = 'none';
                        timerTimestamp.textContent = '';
                    }
                    return;
                }
                // Update countdown display in idle mode
                if (appState === STATE.IDLE) {
                    updateCountdownDisplay();
                }
            }
        }, 1000);
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
