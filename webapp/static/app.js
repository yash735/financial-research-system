/* Financial Research System — browser client.
 *
 * Three jobs: manage uploaded documents, stream a chat turn, and draw the agent
 * trace. The trace is the point — without it a multi-agent system looks exactly
 * like one slow chatbot.
 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var el = {
    lanes: $('lanes'), formatterRow: $('formatter-row'), brandSub: $('brand-sub'),
    dropzone: $('dropzone'), fileInput: $('file-input'), doclist: $('doclist'),
    docCount: $('doc-count'), railHint: $('rail-hint'),
    transcript: $('transcript'), empty: $('empty'), suggestions: $('suggestions'),
    input: $('input'), send: $('btn-send'),
    trace: $('trace'), traceBody: $('trace-body'), traceTotal: $('trace-total'),
    gathered: $('gathered'), gatheredText: $('gathered-text'),
    shell: $('shell'), overlay: $('drop-overlay'), toasts: $('toasts'),
    btnTheme: $('btn-theme'), btnTrace: $('btn-trace')
  };

  var state = {
    sessionId: null,
    caps: null,
    docs: [],
    streaming: false,
    abort: null,
    pollTimer: null,
    liveTimer: null,
    turnStart: 0
  };

  var SUGGESTIONS = [
    'What were total revenues and the operating margin in the most recent fiscal year? Cite pages.',
    'Summarise the main risk factors in this filing.',
    'How do the reported fundamentals compare to how the stock is trading right now?'
  ];

  /* ── utilities ─────────────────────────────────────────────────────── */
  function svg(id, cls) {
    return '<svg class="icon ' + (cls || '') + '"><use href="#' + id + '"/></svg>';
  }

  function fmtMs(ms) {
    if (ms == null) return '';
    return ms < 1000 ? ms + 'ms' : (ms / 1000).toFixed(1) + 's';
  }

  function fmtNum(n) {
    return (n || 0).toLocaleString('en-US');
  }

  function toast(message) {
    var node = document.createElement('div');
    node.className = 'toast';
    node.innerHTML = svg('i-alert') + '<span></span>';
    node.querySelector('span').textContent = message;
    el.toasts.appendChild(node);
    setTimeout(function () { node.remove(); }, 7000);
  }

  function atBottom() {
    var t = el.transcript;
    return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
  }

  function scrollDown(force) {
    if (force || atBottom()) el.transcript.scrollTop = el.transcript.scrollHeight;
  }

  /* ── capabilities ──────────────────────────────────────────────────── */
  function renderCaps(caps) {
    state.caps = caps;
    var order = ['documents', 'datastore', 'market'];
    el.lanes.innerHTML = order.map(function (key) {
      var lane = caps.lanes[key];
      var cls = lane.enabled ? 'lane on' : 'lane off';
      var title = lane.enabled ? '' : ' title="' + (lane.reason || 'Unavailable') + '"';
      return '<li class="' + cls + '"' + title + '><span class="dot"></span>' +
        '<span class="lane-text"><b>' + lane.label + '</b><small>' + lane.detail + '</small></span></li>';
    }).join('');

    var f = caps.formatter;
    el.formatterRow.className = 'formatter-row' + (f.mode === 'local' ? ' fallback' : '');
    el.formatterRow.innerHTML = '<span class="dot"></span><span>' + f.label + '</span>';
    if (f.mode === 'local' && f.error) el.formatterRow.title = f.error;

    el.brandSub.textContent = caps.model + ' · Document AI · Vertex AI';

    if (!caps.lanes.documents.enabled) {
      el.dropzone.disabled = true;
      el.railHint.textContent = caps.lanes.documents.reason || 'Uploads unavailable.';
    } else {
      el.railHint.textContent = 'Up to ' + caps.limits.max_docs + ' documents, ' +
        caps.limits.max_upload_mb + ' MB each. OCR\'d with Document AI and held in memory for this session only.';
    }
  }

  /* ── documents ─────────────────────────────────────────────────────── */
  function renderDocs(docs) {
    state.docs = docs || [];
    el.docCount.textContent = state.docs.length ? state.docs.length + ' loaded' : '';

    el.doclist.innerHTML = state.docs.map(function (d) {
      var meta, bar = '';
      if (d.status === 'processing') {
        var pct = d.pages_total ? Math.round(100 * d.pages_done / d.pages_total) : 0;
        meta = 'OCR ' + (d.pages_total ? d.pages_done + '/' + d.pages_total + ' pages' : 'starting…');
        bar = '<div class="bar"><i style="width:' + pct + '%"></i></div>';
      } else if (d.status === 'failed') {
        meta = d.error || 'Failed';
      } else {
        meta = d.page_count + ' pages · ' + fmtNum(d.char_count) + ' chars';
      }
      return '<li class="docitem ' + d.status + '">' +
        '<div class="doc-row">' + svg('i-doc') +
        '<span class="doc-name" title="' + md.escape(d.filename) + '">' + md.escape(d.filename) + '</span>' +
        '<button class="doc-del" data-id="' + d.doc_id + '" title="Remove">' + svg('i-trash') + '</button></div>' +
        '<div class="doc-meta' + (d.status === 'failed' ? ' err' : '') + '">' + md.escape(meta) + '</div>' + bar +
        '</li>';
    }).join('');

    var anyProcessing = state.docs.some(function (d) { return d.status === 'processing'; });
    if (anyProcessing) schedulePoll(); else clearTimeout(state.pollTimer);
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(refreshDocs, 1000);
  }

  function refreshDocs() {
    if (!state.sessionId) return;
    fetch('/api/documents?session_id=' + state.sessionId)
      .then(function (r) { return r.json(); })
      .then(function (d) { renderDocs(d.documents); })
      .catch(function () { /* transient; the next poll retries */ });
  }

  function upload(files) {
    if (!files || !files.length) return;
    if (state.caps && !state.caps.lanes.documents.enabled) {
      toast(state.caps.lanes.documents.reason || 'Uploads are unavailable.');
      return;
    }
    var form = new FormData();
    form.append('session_id', state.sessionId || '');
    for (var i = 0; i < files.length; i++) form.append('files', files[i]);

    fetch('/api/documents', { method: 'POST', body: form })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var b = res.body;
        if (b.session_id) state.sessionId = b.session_id;
        (b.rejected || []).forEach(function (r) { toast(r.filename + ': ' + r.error); });
        if (b.error) toast(b.error);
        refreshDocs();
      })
      .catch(function (e) { toast('Upload failed: ' + e.message); });
  }

  /* ── transcript ────────────────────────────────────────────────────── */
  function addUserTurn(text) {
    if (el.empty) el.empty.style.display = 'none';
    var node = document.createElement('div');
    node.className = 'turn user';
    node.innerHTML = '<div class="bubble"></div>';
    node.querySelector('.bubble').textContent = text;
    el.transcript.appendChild(node);
    scrollDown(true);
  }

  function addBotTurn() {
    var node = document.createElement('div');
    node.className = 'turn bot';
    node.innerHTML =
      '<div class="who">Analyst <span class="thinking"><i></i><i></i><i></i></span></div>' +
      '<div class="md"></div>';
    el.transcript.appendChild(node);
    scrollDown(true);
    return {
      root: node,
      body: node.querySelector('.md'),
      thinking: node.querySelector('.thinking')
    };
  }

  /* ── trace ─────────────────────────────────────────────────────────── */
  function resetTrace() {
    el.traceBody.innerHTML = '';
    el.traceTotal.textContent = '';
    el.gathered.hidden = true;
    el.gathered.open = false;
    el.gatheredText.textContent = '';
  }

  function traceNode(label) {
    var node = document.createElement('div');
    node.className = 'node live';
    node.innerHTML = '<div class="node-head"><span>' + md.escape(label) +
      '</span><span class="node-time"></span></div>';
    el.traceBody.appendChild(node);
    el.traceBody.scrollTop = el.traceBody.scrollHeight;
    return node;
  }

  function traceCall(node, data) {
    var call = document.createElement('div');
    call.className = 'call pending';
    call.dataset.tool = data.tool;
    var args = Object.keys(data.args || {}).map(function (k) {
      return k + ': ' + data.args[k];
    }).join('\n');
    call.innerHTML =
      '<div class="call-head">' + svg('i-arrow', 'spin') +
      '<span class="call-name">' + md.escape(data.tool_label || data.tool) + '</span>' +
      '<span class="call-ms"></span>' + (args ? svg('i-chevron') : '') + '</div>' +
      (args ? '<div class="call-body"></div>' : '');
    if (args) {
      call.querySelector('.call-body').textContent = args;
      call.querySelector('.call-head').addEventListener('click', function () {
        call.classList.toggle('open');
      });
    }
    node.appendChild(call);
    el.traceBody.scrollTop = el.traceBody.scrollHeight;
    return call;
  }

  function finishCall(call, data) {
    call.classList.remove('pending');
    call.classList.add(data.ok ? 'ok' : 'fail');
    var icon = call.querySelector('.call-head > .icon');
    icon.classList.remove('spin');
    icon.querySelector('use').setAttribute('href', data.ok ? '#i-check' : '#i-alert');
    call.querySelector('.call-ms').textContent = fmtMs(data.latency_ms);
    if (data.preview) {
      var body = call.querySelector('.call-body');
      if (!body) {
        body = document.createElement('div');
        body.className = 'call-body';
        call.appendChild(body);
        call.querySelector('.call-head').addEventListener('click', function () {
          call.classList.toggle('open');
        });
      }
      body.textContent = (body.textContent ? body.textContent + '\n\n' : '') + data.preview;
    }
  }

  // A 15-second tool call must not look frozen: tick the elapsed time while it runs.
  function startLiveTimer() {
    clearInterval(state.liveTimer);
    state.liveTimer = setInterval(function () {
      var ms = Date.now() - state.turnStart;
      el.traceTotal.textContent = fmtMs(ms);
      var pending = el.traceBody.querySelectorAll('.call.pending .call-ms');
      for (var i = 0; i < pending.length; i++) pending[i].textContent = fmtMs(ms);
    }, 100);
  }

  /* ── chat ──────────────────────────────────────────────────────────── */
  function setStreaming(on) {
    state.streaming = on;
    document.body.classList.toggle('streaming', on);
    el.send.disabled = false;
  }

  function send(text) {
    text = (text || el.input.value).trim();
    if (!text) return;
    if (state.streaming) return;

    el.input.value = '';
    autosize();
    addUserTurn(text);
    var turn = addBotTurn();
    resetTrace();
    state.turnStart = Date.now();
    setStreaming(true);
    startLiveTimer();

    var nodes = {};       // agent -> trace node
    var calls = {};       // tool key -> call element
    var answer = '';
    var lastError = '';
    var controller = new AbortController();
    state.abort = controller;

    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, message: text }),
      signal: controller.signal
    }).then(function (res) {
      if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';

      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { drain(true); return finish(); }
          // NORMALISE LINE ENDINGS FIRST. sse_starlette separates frames with
          // CRLF, so splitting on "\n\n" alone never matches and no frame is
          // ever completed — the stream silently produces nothing.
          buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, '\n');
          drain(false);
          return pump();
        });
      }

      // SSE frames are separated by a blank line. Keep the trailing partial
      // frame in the buffer unless the stream has ended.
      function drain(final) {
        var frames = buffer.split('\n\n');
        buffer = final ? '' : frames.pop();
        frames.forEach(function (frame) {
          frame.split('\n').forEach(function (line) {
            if (line.indexOf('data:') !== 0) return;
            var payload = line.slice(5).trim();
            if (!payload) return;
            try { handle(JSON.parse(payload)); } catch (e) { /* keepalive or partial */ }
          });
        });
      }

      function handle(ev) {
        var d = ev.data || {};
        switch (ev.type) {
          case 'agent_start':
            if (!nodes[ev.agent]) nodes[ev.agent] = traceNode(ev.label || ev.agent);
            Object.keys(nodes).forEach(function (k) {
              if (k !== ev.agent) nodes[k].classList.remove('live');
            });
            break;
          case 'tool_call': {
            var node = nodes[ev.agent] || (nodes[ev.agent] = traceNode(ev.label || ev.agent));
            calls[d.tool + ':' + ev.seq] = traceCall(node, d);
            calls['_last:' + d.tool] = calls[d.tool + ':' + ev.seq];
            break;
          }
          case 'tool_result': {
            var call = calls['_last:' + d.tool];
            if (call) finishCall(call, d);
            break;
          }
          case 'answer_delta':
            answer += d.text;
            turn.thinking.style.display = 'none';
            turn.body.innerHTML = md.render(answer);
            scrollDown();
            break;
          case 'answer_done':
            answer = d.text;
            turn.thinking.style.display = 'none';
            turn.body.innerHTML = md.render(answer);
            scrollDown();
            break;
          case 'gathered':
            el.gathered.hidden = false;
            el.gatheredText.textContent = d.text;
            break;
          case 'error':
            // Surface it in the transcript as well as a toast. A bare "no
            // answer" with the reason only in a toast that fades away is not
            // debuggable, and quota exhaustion is the likeliest demo failure.
            lastError = d.message || 'Something went wrong.';
            toast(lastError);
            break;
          case 'done':
            if (d.answer && !answer) {
              turn.body.innerHTML = md.render(d.answer);
            }
            el.traceTotal.textContent = fmtMs(d.elapsed_ms);
            break;
        }
      }

      function finish() {
        clearInterval(state.liveTimer);
        Object.keys(nodes).forEach(function (k) { nodes[k].classList.remove('live'); });
        turn.thinking.style.display = 'none';
        if (!turn.body.innerHTML) {
          turn.body.innerHTML = lastError
            ? '<p class="turn-error">' + md.escape(lastError) + '</p>'
            : '<p style="color:var(--text-mute)">No answer was returned.</p>';
        }
        setStreaming(false);
        state.abort = null;
        scrollDown();
      }

      return pump();
    }).catch(function (e) {
      clearInterval(state.liveTimer);
      turn.thinking.style.display = 'none';
      if (e.name === 'AbortError') {
        if (!turn.body.innerHTML) {
          turn.body.innerHTML = '<p style="color:var(--text-mute)">Stopped.</p>';
        }
      } else {
        toast('Request failed: ' + e.message);
        turn.root.remove();
      }
      setStreaming(false);
      state.abort = null;
    });
  }

  /* ── composer ──────────────────────────────────────────────────────── */
  function autosize() {
    el.input.style.height = 'auto';
    el.input.style.height = Math.min(el.input.scrollHeight, 180) + 'px';
  }

  /* ── wiring ────────────────────────────────────────────────────────── */
  function init() {
    // theme
    var saved = localStorage.getItem('frs-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
    else if (window.matchMedia && !window.matchMedia('(prefers-color-scheme: dark)').matches) {
      document.documentElement.setAttribute('data-theme', 'light');
    }
    el.btnTheme.addEventListener('click', function () {
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('frs-theme', next);
    });

    // trace toggle
    el.btnTrace.classList.add('on');
    el.btnTrace.addEventListener('click', function () {
      var hidden = el.shell.classList.toggle('no-trace');
      el.shell.classList.toggle('show-trace', !hidden);
      el.btnTrace.classList.toggle('on', !hidden);
    });

    // suggestions
    el.suggestions.innerHTML = SUGGESTIONS.map(function (s) {
      return '<button class="chip">' + svg('i-arrow') + '<span>' + md.escape(s) + '</span></button>';
    }).join('');
    el.suggestions.addEventListener('click', function (e) {
      var chip = e.target.closest('.chip');
      if (chip) send(chip.querySelector('span').textContent);
    });

    // composer
    el.input.addEventListener('input', autosize);
    el.input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    el.send.addEventListener('click', function () {
      if (state.streaming && state.abort) state.abort.abort();
      else send();
    });

    // uploads
    el.dropzone.addEventListener('click', function () { el.fileInput.click(); });
    el.fileInput.addEventListener('change', function () {
      upload(el.fileInput.files);
      el.fileInput.value = '';
    });
    el.doclist.addEventListener('click', function (e) {
      var btn = e.target.closest('.doc-del');
      if (!btn) return;
      fetch('/api/documents/' + btn.dataset.id + '?session_id=' + state.sessionId, { method: 'DELETE' })
        .then(function (r) { return r.json(); })
        .then(function (d) { renderDocs(d.documents); })
        .catch(function (e2) { toast('Could not remove: ' + e2.message); });
    });

    // drag and drop anywhere on the page
    var depth = 0;
    window.addEventListener('dragenter', function (e) {
      e.preventDefault();
      if (++depth === 1) el.overlay.classList.add('show');
    });
    window.addEventListener('dragover', function (e) { e.preventDefault(); });
    window.addEventListener('dragleave', function () {
      if (--depth <= 0) { depth = 0; el.overlay.classList.remove('show'); }
    });
    window.addEventListener('drop', function (e) {
      e.preventDefault();
      depth = 0;
      el.overlay.classList.remove('show');
      if (e.dataTransfer && e.dataTransfer.files.length) upload(e.dataTransfer.files);
    });

    // paste a file straight in
    window.addEventListener('paste', function (e) {
      if (!e.clipboardData || !e.clipboardData.files.length) return;
      if (document.activeElement === el.input && !e.clipboardData.files.length) return;
      upload(e.clipboardData.files);
    });

    // shortcuts
    window.addEventListener('keydown', function (e) {
      if (e.key === '/' && document.activeElement !== el.input) {
        e.preventDefault(); el.input.focus();
      } else if ((e.metaKey || e.ctrlKey) && e.key === '\\') {
        e.preventDefault(); el.btnTrace.click();
      }
    });

    // boot
    fetch('/api/health').then(function (r) { return r.json(); })
      .then(renderCaps)
      .catch(function () { toast('Could not reach the server.'); });

    // ?session=<id> attaches to an existing session instead of minting a new
    // one. Useful for reattaching after a reload, and for driving the UI from a
    // script when capturing screenshots.
    var fromUrl = new URLSearchParams(location.search).get('session');
    if (fromUrl) {
      state.sessionId = fromUrl;
      refreshDocs();
    } else {
      fetch('/api/session', { method: 'POST' }).then(function (r) { return r.json(); })
        .then(function (d) { state.sessionId = d.session_id; })
        .catch(function () { /* a session is minted on first use anyway */ });
    }

    el.input.focus();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
