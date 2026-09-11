const guideContent = document.querySelector('#guide-content');
const copyGuideButton = document.querySelector('#copy-guide');
const copyStatus = document.querySelector('#copy-status');
const nav = document.querySelector('#guide-nav');
const search = document.querySelector('#guide-search');
const searchStatus = document.querySelector('#search-status');
let guideText = '';
let sectionEntries = [];
let activeSectionId = '';
let sectionObserver = null;
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
// Render text nodes only; never execute Markdown's raw HTML.
function inline(parent, text) {
  text.split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^\)]+\))/g).forEach(part => {
    if (part.startsWith('`') && part.endsWith('`')) parent.append(node('code', part.slice(1, -1)));
    else if (part.startsWith('**') && part.endsWith('**')) parent.append(node('strong', part.slice(2, -2)));
    else {
      const link = /^\[([^\]]+)\]\(([^\)]+)\)$/.exec(part);
      if (link && /^(?:https?:|\/|#)/.test(link[2])) {
        const anchor = node('a', link[1]);
        anchor.href = link[2];
        if (/^https?:/.test(link[2])) { anchor.target = '_blank'; anchor.rel = 'noopener'; }
        parent.append(anchor);
      } else parent.append(document.createTextNode(part));
    }
  });
}
async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    copyStatus.textContent = 'Copied to clipboard.';
    const original = button.textContent;
    button.textContent = 'Copied';
    window.setTimeout(() => { button.textContent = original; }, 1600);
  } catch {
    copyStatus.textContent = 'Clipboard unavailable. Open raw text to select and copy manually.';
  }
}
function render(text) {
  guideContent.replaceChildren(); nav.replaceChildren(); sectionEntries = [];
  const lines = text.split('\n');
  let section = node('section', undefined, 'guide-section');
  guideContent.append(section);
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    if (line.startsWith('```')) {
      const language = line.slice(3).trim() || 'text'; const codeLines = [];
      while (++index < lines.length && !lines[index].startsWith('```')) codeLines.push(lines[index]);
      index++;
      const box = node('div', undefined, 'guide-code');
      const bar = node('div', undefined, 'guide-codebar');
      const button = node('button', 'Copy'); button.type = 'button';
      button.setAttribute('aria-label', `Copy ${language} example`);
      button.addEventListener('click', () => copy(codeLines.join('\n'), button));
      bar.append(node('span', language), button);
      const pre = node('pre'); pre.tabIndex = 0;
      pre.append(node('code', codeLines.join('\n')));
      box.append(bar, pre); section.append(box); continue;
    }
    const heading = /^(#{1,3}) (.+)$/.exec(line);
    if (heading) {
      const depth = heading[1].length;
      if (depth === 2) {
        section = node('section', undefined, 'guide-section');
        const number = /^(\d+)\./.exec(heading[2]);
        section.id = number ? `section-${number[1]}` : `part-${sectionEntries.length}`;
        guideContent.append(section);
        const link = node('a', heading[2]); link.href = `#${section.id}`;
        nav.append(link); sectionEntries.push({section, link});
      }
      section.append(node(depth === 3 ? 'h3' : 'h2', heading[2])); index++; continue;
    }
    if (line.startsWith('|') && lines[index + 1]?.match(/^\|[\s:|\-]+\|$/)) {
      const wrapper = node('div', undefined, 'guide-table'); wrapper.tabIndex = 0;
      const table = node('table');
      const cells = value => value.trim().slice(1, -1).split('|').map(c => c.trim());
      const head = node('thead'); const row = node('tr');
      cells(line).forEach(value => { const cell = node('th'); cell.scope = 'col'; inline(cell, value); row.append(cell); });
      head.append(row); table.append(head); index += 2;
      const body = node('tbody');
      while (lines[index]?.startsWith('|')) {
        const tr = node('tr');
        cells(lines[index++]).forEach(value => { const td = node('td'); inline(td, value); tr.append(td); });
        body.append(tr);
      }
      table.append(body); wrapper.append(table); section.append(wrapper); continue;
    }
    if (/^(?:- |\d+\. )/.test(line)) {
      const list = node(/^\d+\./.test(line) ? 'ol' : 'ul');
      while (index < lines.length && /^(?:- |\d+\. )/.test(lines[index])) {
        let value = lines[index++].replace(/^(?:- |\d+\. )/, '');
        while (index < lines.length && /^ {2,}\S/.test(lines[index])) value += ' ' + lines[index++].trim();
        const item = node('li'); inline(item, value); list.append(item);
      }
      section.append(list); continue;
    }
    const paragraph = [lines[index++]];
    while (index < lines.length && lines[index].trim() && !/^(#|```|\||- |\d+\. )/.test(lines[index])) paragraph.push(lines[index++]);
    const p = node('p'); inline(p, paragraph.join(' ')); section.append(p);
  }
  syncNav();
  sectionObserver?.disconnect();
  sectionObserver = new IntersectionObserver((entries) => {
    const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    if (!visible.length || location.hash) return;
    activeSectionId = visible[0].target.id;
    syncNav();
  }, { rootMargin: '-12% 0px -70% 0px', threshold: 0 });
  sectionEntries.forEach(({ section }) => sectionObserver.observe(section));
}
function syncNav() {
  const selected = location.hash || (activeSectionId ? `#${activeSectionId}` : '');
  sectionEntries.forEach(({section, link}) => {
    if (`#${section.id}` === selected) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  });
}
search.addEventListener('input', () => {
  const query = search.value.trim().toLowerCase(); let count = 0;
  sectionEntries.forEach(({section, link}) => {
    const match = !query || section.textContent.toLowerCase().includes(query);
    link.hidden = !match;
    if (match) {
      count++;
      const text = section.textContent.replace(/\s+/g, ' ').trim();
      link.title = query ? text.slice(Math.max(0, text.toLowerCase().indexOf(query) - 60), 220) : '';
    } else link.removeAttribute('title');
  });
  searchStatus.textContent = query ? `${count} matching section${count === 1 ? '' : 's'}` : '';
  if (query) nav.querySelector('a:not([hidden])')?.focus();
});
window.addEventListener('hashchange', syncNav);
copyGuideButton.addEventListener('click', () => { if (guideText) copy(guideText, copyGuideButton); });
async function loadGuide() {
  try {
    const response = await fetch('/api/docs/external', { cache: 'no-store' });
    if (!response.ok) throw new Error('The integration guide is unavailable. Reload to try again.');
    guideText = await response.text(); render(guideText); copyGuideButton.disabled = false;
    document.getElementById(location.hash.slice(1))?.scrollIntoView();
  } catch (error) {
    guideContent.textContent = error instanceof Error ? error.message : 'Unable to load the guide. Reload to try again.';
  }
}
loadGuide();
