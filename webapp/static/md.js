/* Minimal Markdown renderer.
 *
 * Deliberately hand-rolled rather than pulled from a CDN: a strict-offline page
 * cannot break on bad wifi during a demo, and the formatter only emits a narrow
 * subset of Markdown. Tables ARE included — the analyst reaches for them
 * constantly and a raw pipe table looks broken.
 *
 * Everything is escaped before any markup is inserted, so model output cannot
 * inject HTML into the page.
 */
(function (global) {
  'use strict';

  function esc(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Parks code spans while the rest of the inline markup is processed. Written
  // as an escape rather than a literal control byte: a raw NUL in the source
  // makes git treat this file as binary, so diffs stop rendering. Anything
  // printable would misfire on ordinary text such as "revenue of 680985".
  var SENTINEL = '\u0000';

  // Inline: code first, so its contents are not further transformed.
  function inline(s) {
    // Strip any sentinel already present in the input so it cannot be forged.
    var out = esc(s).split(SENTINEL).join('');
    var codes = [];
    out = out.replace(/`([^`]+)`/g, function (_, c) {
      codes.push(c);
      return SENTINEL + (codes.length - 1) + SENTINEL;
    });
    out = out.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    out = out.replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>');
    out = out.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    out = out.replace(new RegExp(SENTINEL + '(\\d+)' + SENTINEL, 'g'), function (_, i) {
      return '<code>' + codes[+i] + '</code>';
    });
    return out;
  }

  function isTableSep(line) {
    return /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.indexOf('-') !== -1;
  }

  function cells(line) {
    var t = line.trim();
    if (t.charAt(0) === '|') t = t.slice(1);
    if (t.charAt(t.length - 1) === '|') t = t.slice(0, -1);
    return t.split('|').map(function (c) { return c.trim(); });
  }

  function render(src) {
    if (!src) return '';
    var lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    var html = [];
    var i = 0;

    while (i < lines.length) {
      var line = lines[i];

      // fenced code
      var fence = line.match(/^\s*```(\w*)\s*$/);
      if (fence) {
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
        i++;
        html.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>');
        continue;
      }

      // table: a header row followed by a separator row
      if (line.indexOf('|') !== -1 && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        var head = cells(line);
        i += 2;
        var body = [];
        while (i < lines.length && lines[i].indexOf('|') !== -1 && lines[i].trim()) {
          body.push(cells(lines[i++]));
        }
        var t = '<div class="tablewrap"><table><thead><tr>';
        head.forEach(function (h) { t += '<th>' + inline(h) + '</th>'; });
        t += '</tr></thead><tbody>';
        body.forEach(function (row) {
          t += '<tr>';
          for (var c = 0; c < head.length; c++) t += '<td>' + inline(row[c] || '') + '</td>';
          t += '</tr>';
        });
        html.push(t + '</tbody></table></div>');
        continue;
      }

      // heading
      var h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        var lvl = Math.min(h[1].length, 3);
        html.push('<h' + lvl + '>' + inline(h[2]) + '</h' + lvl + '>');
        i++;
        continue;
      }

      // horizontal rule
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { html.push('<hr>'); i++; continue; }

      // blockquote
      if (/^\s*>\s?/.test(line)) {
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ''));
        html.push('<blockquote>' + render(q.join('\n')) + '</blockquote>');
        continue;
      }

      // lists (one level; nesting is rare in this output and not worth the code)
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        var ordered = /^\s*\d+\./.test(line);
        var items = [];
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          var item = lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, '');
          // continuation lines belong to the current item
          while (i < lines.length && lines[i].trim() && !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
                 /^\s{2,}/.test(lines[i])) {
            item += ' ' + lines[i++].trim();
          }
          items.push('<li>' + inline(item) + '</li>');
        }
        var tag = ordered ? 'ol' : 'ul';
        html.push('<' + tag + '>' + items.join('') + '</' + tag + '>');
        continue;
      }

      // blank
      if (!line.trim()) { i++; continue; }

      // paragraph
      var para = [];
      while (i < lines.length && lines[i].trim() &&
             !/^\s*(#{1,6}\s|```|>|([-*+]|\d+\.)\s)/.test(lines[i]) &&
             !(lines[i].indexOf('|') !== -1 && i + 1 < lines.length && isTableSep(lines[i + 1]))) {
        para.push(lines[i++]);
      }
      if (para.length) html.push('<p>' + inline(para.join('\n')).replace(/\n/g, '<br>') + '</p>');
      else i++;
    }

    return html.join('');
  }

  global.md = { render: render, escape: esc };
})(window);
