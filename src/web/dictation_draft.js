/* In-memory recognition ownership. Editing a changing line freezes the draft. */
(function (scope) {
  'use strict';
  function textPatch(before, after) {
    let prefix = 0, suffix = 0;
    while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix++;
    while (suffix < before.length - prefix && suffix < after.length - prefix
      && before[before.length - suffix - 1] === after[after.length - suffix - 1]) suffix++;
    if (prefix === before.length && prefix === after.length) return null;
    return { start: prefix, end: before.length - suffix, text: after.slice(prefix, after.length - suffix) };
  }
  class ManualJournal {
    constructor(text = '', { maxEntries = 100, maxUnits = 131072 } = {}) {
      this.maxEntries = maxEntries; this.maxUnits = maxUnits; this.serial = 0; this.reset(text);
    }
    makeNodes(text) { return text.split('').map(character => ({ id: ++this.serial, character, visible: true })); }
    get text() { return this.nodes.filter(node => node.visible).map(node => node.character).join(''); }
    reset(text = '') {
      this.nodes = this.makeNodes(text); this.undoEntries = []; this.redoEntries = [];
      this.group = null; this.enabled = false;
    }
    barrier() { this.group = null; }
    point(offset, visible) {
      const at = Math.max(0, Math.min(visible.length, Number.isInteger(offset) ? offset : 0));
      return { left: visible[at - 1] || null, right: visible[at] || null, fallback: at };
    }
    selection(value, visible = this.nodes.filter(node => node.visible)) {
      return { start: this.point(value.start, visible), end: this.point(value.end, visible),
        direction: ['forward', 'backward'].includes(value.direction) ? value.direction : 'none' };
    }
    position(point) {
      let index = point.right ? this.nodes.indexOf(point.right) : -1;
      if (index < 0 && point.left) { index = this.nodes.indexOf(point.left); if (index >= 0) index++; }
      if (index < 0) return Math.min(this.nodes.filter(node => node.visible).length, point.fallback);
      let at = 0;
      for (let i = 0; i < index; i++) if (this.nodes[i].visible) at++;
      return at;
    }
    restoreSelection(value) {
      return { start: this.position(value.start), end: this.position(value.end), direction: value.direction };
    }
    mutate(patch) {
      const visible = this.nodes.filter(node => node.visible);
      if (!Number.isInteger(patch.start) || !Number.isInteger(patch.end)
          || patch.start < 0 || patch.end < patch.start || patch.end > visible.length) throw new RangeError('Invalid draft patch');
      const at = patch.start < visible.length ? this.nodes.indexOf(visible[patch.start]) : this.nodes.length;
      const removed = visible.slice(patch.start, patch.end), inserted = this.makeNodes(patch.text);
      for (const node of removed) node.visible = false;
      this.nodes = this.nodes.slice(0, at).concat(inserted, this.nodes.slice(at));
      return { removed, inserted };
    }
    collect() {
      const retained = new Set();
      for (const entry of this.undoEntries.concat(this.redoEntries)) {
        for (const node of entry.removed.concat(entry.inserted)) retained.add(node);
        for (const selection of [entry.beforeSelection, entry.afterSelection]) {
          for (const point of [selection.start, selection.end]) {
            if (point.left) retained.add(point.left); if (point.right) retained.add(point.right);
          }
        }
      }
      this.nodes = this.nodes.filter(node => node.visible || retained.has(node));
    }
    enginePatch(patch) {
      if (!patch) return;
      this.mutate(patch); this.enabled = true; this.collect();
    }
    record(text, beforeSelection, afterSelection, inputType = 'manual', now = Date.now()) {
      const patch = textPatch(this.text, text);
      if (!patch) return false;
      const visible = this.nodes.filter(node => node.visible);
      const before = this.selection(beforeSelection || { start: patch.start, end: patch.end }, visible);
      const previous = this.group;
      let merge = false;
      if (previous && now >= previous.time && now - previous.time <= 1000 && previous.inputType === inputType
          && this.undoEntries[this.undoEntries.length - 1] === previous && !this.redoEntries.length) {
        if (inputType === 'insertText' && patch.end === patch.start && previous.inserted.length) {
          const last = visible.indexOf(previous.inserted[previous.inserted.length - 1]);
          merge = last >= 0 && patch.start === last + 1;
        } else if (inputType === 'deleteContentBackward' && !patch.text) {
          merge = patch.end === this.restoreSelection(previous.afterSelection).start;
        } else if (inputType === 'deleteContentForward' && !patch.text) {
          merge = patch.start === this.restoreSelection(previous.afterSelection).start;
        }
      }
      const mutation = this.mutate(patch);
      const after = this.selection(afterSelection || { start: patch.start + patch.text.length, end: patch.start + patch.text.length });
      this.redoEntries = [];
      if (merge) {
        previous.removed = previous.removed.concat(mutation.removed);
        previous.inserted = previous.inserted.concat(mutation.inserted);
        previous.afterSelection = after; previous.time = now;
      } else {
        this.undoEntries.push(Object.assign(mutation, { beforeSelection: before, afterSelection: after, inputType, time: now }));
      }
      const latest = this.undoEntries[this.undoEntries.length - 1];
      this.group = ['insertText', 'deleteContentBackward', 'deleteContentForward'].includes(inputType) ? latest : null;
      let units = this.undoEntries.reduce((sum, entry) => sum + entry.removed.length + entry.inserted.length, 0);
      while (this.undoEntries.length > 1 && (this.undoEntries.length > this.maxEntries || units > this.maxUnits)) {
        const removed = this.undoEntries.shift(); units -= removed.removed.length + removed.inserted.length;
      }
      this.collect(); return true;
    }
    undo() {
      const entry = this.undoEntries.pop(); if (!entry) return null;
      for (const node of entry.inserted) node.visible = false;
      for (const node of entry.removed) node.visible = true;
      this.redoEntries.push(entry); this.group = null; this.collect();
      return { text: this.text, selection: this.restoreSelection(entry.beforeSelection) };
    }
    redo() {
      const entry = this.redoEntries.pop(); if (!entry) return null;
      for (const node of entry.removed) node.visible = false;
      for (const node of entry.inserted) node.visible = true;
      this.undoEntries.push(entry); this.group = null; this.collect();
      return { text: this.text, selection: this.restoreSelection(entry.afterSelection) };
    }
    nativeHistory(text, direction) {
      if (text === this.text) return;
      const stack = direction === 'undo' ? this.undoEntries : this.redoEntries;
      while (stack.length) {
        const value = this[direction](); if (value.text === text) return;
      }
      // Native grouping can differ before dictation takes ownership of history.
      // A mismatch becomes a new baseline, never a guessed inverse operation.
      this.reset(text);
    }
  }
  class DraftModel {
    constructor(text = '') { this.text = text; this.spans = new Map(); this.retired = new Set(); this.take = false; this.epoch = 0; }
    beginTake(epoch = 1) { this.spans.clear(); this.retired.clear(); this.take = true; this.epoch = epoch; }
    advanceEpoch(epoch) {
      for (const span of this.spans.values()) {
        if (span.epoch !== epoch) { span.frozen = true; this.retired.add(span.key); }
      }
      this.epoch = epoch;
    }
    commitTake() { this.spans.clear(); this.retired.clear(); this.take = false; this.epoch = 0; }
    reset(text = '') { this.text = text; this.commitTake(); }
    replace(start, end, text) {
      const previous = this.text.slice(start, end);
      let prefix = 0, suffix = 0;
      while (prefix < previous.length && prefix < text.length && previous[prefix] === text[prefix]) prefix++;
      while (suffix < previous.length - prefix && suffix < text.length - prefix
        && previous[previous.length - suffix - 1] === text[text.length - suffix - 1]) suffix++;
      this.text = this.text.slice(0, start) + text + this.text.slice(end);
      if (prefix === previous.length && prefix === text.length) return null;
      return { start: start + prefix, end: end - suffix, text: text.slice(prefix, text.length - suffix) };
    }
    line(event) {
      if (!this.take || event.epoch !== this.epoch) return null;
      const key = `${event.epoch}:${event.lineId}`;
      if (this.retired.has(key)) return null;
      let span = this.spans.get(key);
      if (span && (span.complete || span.frozen)) return null;
      const words = event.text.trim();
      if (!span && !words) return null;
      if (!span) {
        const lead = this.text && !/\s$/.test(this.text) ? ' ' : '';
        span = { key, epoch: event.epoch, start: this.text.length, end: this.text.length,
          lead, complete: false, frozen: false };
        this.spans.set(key, span);
      }
      const text = words ? span.lead + words : '';
      const oldEnd = span.end, delta = text.length - (oldEnd - span.start);
      const patch = this.replace(span.start, oldEnd, text);
      span.end = span.start + text.length;
      span.complete = event.complete;
      for (const other of this.spans.values()) {
        if (other !== span && other.start >= oldEnd) { other.start += delta; other.end += delta; }
      }
      return patch;
    }
    userEdit(text) {
      if (text === this.text) return { changed: false, reset: false };
      let start = 0, suffix = 0;
      while (start < this.text.length && start < text.length && this.text[start] === text[start]) start++;
      while (suffix < this.text.length - start && suffix < text.length - start
        && this.text[this.text.length - suffix - 1] === text[text.length - suffix - 1]) suffix++;
      const end = this.text.length - suffix, delta = text.length - this.text.length;
      const touches = span => start === end
        ? start > span.start && (start < span.end || (!span.complete && start === span.end))
        : start < span.end && end > span.start;
      const needsReset = this.take && [...this.spans.values()].some(span => !span.complete && !span.frozen && touches(span));
      for (const [key, span] of this.spans) {
        if (touches(span)) { this.spans.delete(key); this.retired.add(key); }
        else if (span.start >= end) { span.start += delta; span.end += delta; }
      }
      if (needsReset) {
        // Update authority ends for all visible words. Untouched recognition
        // spans retain their separate provenance so Cancel can still drop them.
        for (const span of this.spans.values()) { span.frozen = true; this.retired.add(span.key); }
        this.epoch = null;
      }
      this.text = text;
      return { changed: true, reset: needsReset };
    }
    provisionalRanges() {
      return [...this.spans.values()].filter(span => !span.complete && !span.frozen && span.epoch === this.epoch && span.end > span.start)
        .map(span => ({ start: Math.min(span.end, span.start + span.lead.length), end: span.end }));
    }
    cancelTake() {
      const patches = [];
      for (const span of [...this.spans.values()].sort((a, b) => b.start - a.start)) {
        patches.push(this.replace(span.start, span.end, ''));
      }
      this.commitTake();
      return patches;
    }
  }
  scope.MotetDictationDraft = { DraftModel, ManualJournal, textPatch };
  if (typeof module !== 'undefined' && module.exports) module.exports = { DraftModel, ManualJournal, textPatch };
})(typeof globalThis !== 'undefined' ? globalThis : this);
