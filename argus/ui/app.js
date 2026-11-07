/**
 * Argus Desktop Application Logic
 * Keyboard-first interaction, heartbeat supervision, and state management.
 */

(function () {
  'use strict';

  // State
  let sessionToken = '';
  let activePort = window.location.port || '8000';
  let heartbeatTimer = null;
  let activeEventSource = null;
  let activeRunId = null;
  let verifiedDiffContent = '';
  let verifiedTargetFile = '';

  // DOM Elements
  const statusIndicator = document.getElementById('connection-status');
  const statusText = document.getElementById('status-text');
  const activePortEl = document.getElementById('active-port');
  const sessionSpendEl = document.getElementById('session-spend');
  const taskForm = document.getElementById('task-form');
  const budgetLimitSlider = document.getElementById('budget-limit');
  const budgetValueEl = document.getElementById('budget-value');
  const runBtn = document.getElementById('run-btn');
  const cancelBtn = document.getElementById('cancel-btn');
  const helpBtn = document.getElementById('help-btn');
  const exitBtn = document.getElementById('exit-btn');
  const shortcutsModal = document.getElementById('shortcuts-modal');
  const closeModalBtn = document.getElementById('close-modal-btn');
  
  // Tabs & Panels
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');
  const emptyWaterfall = document.getElementById('empty-waterfall');
  const waterfallContainer = document.getElementById('waterfall-container');
  const attemptsList = document.getElementById('attempts-list');
  const taskDurationEl = document.getElementById('task-duration');
  
  const emptyDiff = document.getElementById('empty-diff');
  const diffViewerContainer = document.getElementById('diff-viewer-container');
  const diffFileName = document.getElementById('diff-file-name');
  const diffEditor = document.getElementById('diff-editor');
  const diffBadge = document.getElementById('diff-badge');
  const applyDiffBtn = document.getElementById('apply-diff-btn');
  const discardDiffBtn = document.getElementById('discard-diff-btn');

  // =========================================================================
  // 1. Initialization & Bootstrap Token Extraction (ADR-003)
  // =========================================================================
  function initSession() {
    activePortEl.textContent = activePort;

    // Extract ephemeral Bearer token from URL hash (#token=xyz)
    if (window.location.hash.startsWith('#token=')) {
      sessionToken = window.location.hash.replace('#token=', '').trim();
      // Immediately sanitize browser URL bar so token never persists in history
      history.replaceState(null, '', window.location.pathname);
    } else {
      // Fallback: check query param if hash is missing
      const urlParams = new URLSearchParams(window.location.search);
      if (urlParams.has('token')) {
        sessionToken = urlParams.get('token');
      }
    }

    // Start background heartbeat supervision (ADR-002)
    startHeartbeat();

    // Attach lifecycle unload beacon
    window.addEventListener('beforeunload', () => {
      if (sessionToken) {
        // Send exit beacon on window close
        const headers = { type: 'application/json' };
        navigator.sendBeacon('/api/v1/session/exit', JSON.stringify({ token: sessionToken }));
      }
    });
  }

  // =========================================================================
  // 2. Heartbeat Supervision
  // =========================================================================
  function startHeartbeat() {
    sendHeartbeat();
    heartbeatTimer = setInterval(sendHeartbeat, 5000);
  }

  async function sendHeartbeat() {
    try {
      const resp = await fetch('/api/v1/session/heartbeat', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${sessionToken}`,
          'Content-Type': 'application/json'
        }
      });
      if (resp.ok) {
        statusIndicator.classList.remove('error');
        statusText.textContent = 'Connected';
      } else {
        statusIndicator.classList.add('error');
        statusText.textContent = 'Auth Failed';
      }
    } catch (e) {
      statusIndicator.classList.add('error');
      statusText.textContent = 'Offline';
    }
  }

  // =========================================================================
  // 3. UI Event Listeners & Sliders
  // =========================================================================
  budgetLimitSlider.addEventListener('input', (e) => {
    budgetValueEl.textContent = `$${parseFloat(e.target.value).toFixed(2)}`;
  });

  // Tab Switching
  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const targetTab = btn.getAttribute('data-tab');
      tabBtns.forEach(b => b.classList.remove('active'));
      tabContents.forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(targetTab).classList.add('active');
    });
  });

  // Modal Controls
  helpBtn.addEventListener('click', () => shortcutsModal.classList.remove('hidden'));
  closeModalBtn.addEventListener('click', () => shortcutsModal.classList.add('hidden'));
  shortcutsModal.addEventListener('click', (e) => {
    if (e.target === shortcutsModal) shortcutsModal.classList.add('hidden');
  });

  // Exit Button
  exitBtn.addEventListener('click', async () => {
    try {
      await fetch('/api/v1/session/exit', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${sessionToken}` }
      });
    } catch (e) {}
    window.close();
  });

  // Global Keyboard Shortcuts
  window.addEventListener('keydown', (e) => {
    // Ctrl + Enter -> Submit Form
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      taskForm.requestSubmit();
    }
    // Esc -> Close Modal or Cancel
    if (e.key === 'Escape') {
      if (!shortcutsModal.classList.contains('hidden')) {
        shortcutsModal.classList.add('hidden');
      } else if (!cancelBtn.disabled) {
        cancelBtn.click();
      }
    }
    // ? -> Toggle Shortcut Cheat Sheet
    if (e.key === '?' && e.target.tagName !== 'INPUT') {
      e.preventDefault();
      shortcutsModal.classList.toggle('hidden');
    }
    // A -> Approve & Apply Diff (if visible)
    if ((e.key === 'a' || e.key === 'A') && e.target.tagName !== 'INPUT' && !diffViewerContainer.classList.contains('hidden')) {
      e.preventDefault();
      applyDiffBtn.click();
    }
  });

  // =========================================================================
  // 4. Task Submission & Execution Loop
  // =========================================================================
  taskForm.addEventListener('submit', async (e) => {
    e.preventDefault();

    const formData = new FormData(taskForm);
    const payload = {
      target_file: formData.get('target_file'),
      test_command: formData.get('test_command'),
      test_file: formData.get('test_file') || null,
      model: formData.get('model'),
      max_attempts: parseInt(formData.get('max_attempts'), 10),
      budget: parseFloat(formData.get('budget'))
    };

    // UI State: Running
    runBtn.disabled = true;
    cancelBtn.disabled = false;
    emptyWaterfall.classList.add('hidden');
    waterfallContainer.classList.remove('hidden');
    attemptsList.innerHTML = '';
    taskDurationEl.textContent = 'Running...';

    // Switch to waterfall tab
    document.querySelector('[data-tab="waterfall-tab"]').click();

    try {
      const resp = await fetch('/api/v1/recovery/run', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${sessionToken}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || 'Failed to dispatch recovery task');
      }

      const data = await resp.json();
      activeRunId = data.task_id;
      connectEventStream(data.task_id);
    } catch (err) {
      alert(`Error starting recovery: ${err.message}`);
      resetRunState();
    }
  });

  cancelBtn.addEventListener('click', () => {
    if (activeEventSource) {
      activeEventSource.close();
      activeEventSource = null;
    }
    resetRunState();
  });

  function resetRunState() {
    runBtn.disabled = false;
    cancelBtn.disabled = true;
  }

  // =========================================================================
  // 5. Server-Sent Events (SSE) Stream Handler
  // =========================================================================
  function connectEventStream(taskId) {
    if (activeEventSource) {
      activeEventSource.close();
    }

    // Connect to SSE stream passing session token via query or header
    activeEventSource = new EventSource(`/api/v1/recovery/stream/${taskId}?token=${encodeURIComponent(sessionToken)}`);

    activeEventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleStreamEvent(data);
      } catch (e) {
        console.error('Failed to parse SSE payload:', e);
      }
    };

    activeEventSource.onerror = () => {
      activeEventSource.close();
      activeEventSource = null;
      resetRunState();
    };
  }

  function handleStreamEvent(event) {
    switch (event.type) {
      case 'ATTEMPT_START':
        renderAttemptStart(event.payload);
        break;
      case 'ATTEMPT_END':
        renderAttemptEnd(event.payload);
        break;
      case 'RECOVERY_COMPLETE':
        renderRecoveryComplete(event.payload);
        break;
    }
  }

  function renderAttemptStart(payload) {
    const card = document.createElement('div');
    card.className = 'attempt-card';
    card.id = `attempt-card-${payload.attempt}`;
    card.innerHTML = `
      <div class="attempt-header">
        <span class="attempt-title">Attempt ${payload.attempt} of ${payload.max_attempts}</span>
        <span class="status-pill">Executing...</span>
      </div>
      <div class="attempt-body">
        <div class="step-row">
          <span>Inference & Reconciliation:</span>
          <span>In Progress</span>
        </div>
      </div>
    `;
    attemptsList.appendChild(card);
    card.scrollIntoView({ behavior: 'smooth' });
  }

  function renderAttemptEnd(record) {
    const card = document.getElementById(`attempt-card-${record.attempt}`);
    if (!card) return;

    const pass = record.test_passed;
    card.classList.add(pass ? 'pass' : 'fail');

    const statusPill = card.querySelector('.status-pill');
    statusPill.className = `status-pill ${pass ? 'pass' : 'fail'}`;
    statusPill.textContent = pass ? 'PASS' : 'FAIL';

    const tierStr = record.tier_used ? `Tier ${record.tier_used}` : 'None';
    card.querySelector('.attempt-body').innerHTML = `
      <div class="step-row">
        <span>Patch Reconciliation:</span>
        <span>${record.patch_success ? 'Applied (' + tierStr + ')' : 'Refused'}</span>
      </div>
      <div class="step-row">
        <span>Tokens & Cost:</span>
        <span>${record.prompt_tokens}+${record.completion_tokens} toks | $${record.cost_usd.toFixed(5)}</span>
      </div>
      <div class="step-row">
        <span>Execution Latency:</span>
        <span>${record.duration_ms.toFixed(0)}ms</span>
      </div>
      ${!pass && record.error_summary ? `<div class="error-snippet">${escapeHtml(record.error_summary)}</div>` : ''}
    `;

    // Update global spend counter
    if (record.session_spend) {
      sessionSpendEl.textContent = `$${record.session_spend.toFixed(4)}`;
    }
  }

  function renderRecoveryComplete(outcome) {
    resetRunState();
    if (activeEventSource) {
      activeEventSource.close();
      activeEventSource = null;
    }

    taskDurationEl.textContent = `${(outcome.total_duration_ms / 1000).toFixed(1)}s`;

    if (outcome.status === 'SUCCESS' && outcome.unified_diff) {
      verifiedDiffContent = outcome.unified_diff;
      verifiedTargetFile = outcome.target_file;
      renderDiffReview(outcome.target_file, outcome.unified_diff);
    }
  }

  // =========================================================================
  // 6. Diff Review & Patch Application
  // =========================================================================
  function renderDiffReview(targetFile, diffText) {
    emptyDiff.classList.add('hidden');
    diffViewerContainer.classList.remove('hidden');
    diffFileName.textContent = targetFile;
    diffBadge.classList.remove('hidden');

    // Render line-by-line diff
    diffEditor.innerHTML = '';
    const lines = diffText.split('\n');

    lines.forEach((line, idx) => {
      const lineEl = document.createElement('div');
      lineEl.className = 'diff-line';
      if (line.startsWith('+') && !line.startsWith('+++')) {
        lineEl.classList.add('add');
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        lineEl.classList.add('del');
      } else if (line.startsWith('@@')) {
        lineEl.classList.add('hunk');
      }

      lineEl.innerHTML = `
        <span class="line-no">${idx + 1}</span>
        <span class="line-content">${escapeHtml(line)}</span>
      `;
      diffEditor.appendChild(lineEl);
    });

    // Auto-switch to diff tab
    document.getElementById('diff-tab-btn').click();
  }

  applyDiffBtn.addEventListener('click', async () => {
    if (!verifiedTargetFile) return;

    try {
      const resp = await fetch('/api/v1/recovery/apply', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${sessionToken}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          task_id: activeRunId,
          target_file: verifiedTargetFile
        })
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || 'Failed to apply patch');
      }

      alert(`Successfully applied verified patch to ${verifiedTargetFile}!`);
      diffViewerContainer.classList.add('hidden');
      emptyDiff.classList.remove('hidden');
      diffBadge.classList.add('hidden');
    } catch (err) {
      alert(`Patch application error: ${err.message}`);
    }
  });

  discardDiffBtn.addEventListener('click', () => {
    diffViewerContainer.classList.add('hidden');
    emptyDiff.classList.remove('hidden');
    diffBadge.classList.add('hidden');
  });

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // Run on page load
  window.addEventListener('DOMContentLoaded', initSession);
})();
