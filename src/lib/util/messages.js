// Helpers for rendering flattened per-item message lists.

/**
 * Collapse a flattened message list down to unique lines, tagging any line
 * that occurred more than once with its count (`… (×3)`).
 *
 * Plan-style payloads carry one message array per item, and the *same* text
 * routinely repeats across items: `/api/jobs/delete-plan` appends the fixed
 * "Archive input detected; delete-on-verify will remove the entire archive"
 * once per archive source, and the "multiple selections from the same archive"
 * rejection once per offending member. Flattening those arrays therefore hands
 * the view a list with duplicate strings.
 *
 * That matters because these lists render in keyed `{#each … as m (m)}` blocks
 * keyed by the message itself. A key must uniquely identify its item, so a
 * repeated string is a hard Svelte runtime error (`each_key_duplicate`) that
 * unmounts the whole view through `<svelte:boundary>` — the confirmation modal
 * crashes instead of warning. Summarizing keeps the keys unique, drops a wall
 * of identical lines, and preserves the "how many sources" signal the raw list
 * carried.
 *
 * Order follows first occurrence, so the summary reads in plan order.
 *
 * @param {Iterable<string>|null|undefined} messages
 * @returns {string[]} unique messages, repeats suffixed with their count
 */
export function summarizeMessages(messages) {
  const order = [];
  // Null-prototype object rather than a Map: this is a throwaway local, and
  // the svelte-eslint `prefer-svelte-reactivity` rule flags raw Map/Set usage
  // even where reactivity isn't wanted (same workaround as jobs.svelte.js).
  const counts = Object.create(null);
  for (const raw of messages ?? []) {
    const text = typeof raw === 'string' ? raw : String(raw);
    if (counts[text] === undefined) {
      counts[text] = 0;
      order.push(text);
    }
    counts[text] += 1;
  }
  return order.map((text) => (counts[text] > 1 ? `${text} (×${counts[text]})` : text));
}
