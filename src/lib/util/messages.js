// Helpers for rendering flattened per-item message lists.

/**
 * @typedef {object} SummarizedMessage
 * @property {string} key   the raw message — unique across the returned list,
 *                          safe to use as an `{#each}` key
 * @property {string} text  what to render: the raw message, suffixed with its
 *                          occurrence count when it repeated
 * @property {number} count how many times the raw message occurred
 */

/**
 * Collapse a flattened message list to one entry per distinct message, in
 * first-seen order, carrying the number of times each occurred.
 *
 * Plan-style payloads carry one message array per item, and the *same* text
 * routinely repeats across items: `/api/jobs/delete-plan` appends the fixed
 * "Archive input detected; delete-on-verify will remove the entire archive"
 * once per archive source, and the "multiple selections from the same archive"
 * rejection once per offending member. Flattening those arrays therefore hands
 * the view a list with duplicate strings, and a keyed `{#each}` over duplicate
 * keys is a hard Svelte runtime error (`each_key_duplicate`) that unmounts the
 * whole view through `<svelte:boundary>` — the confirmation modal crashes
 * instead of warning.
 *
 * Identity is deliberately kept separate from presentation. `key` is the raw
 * message, which is unique by construction because that is what this function
 * deduplicates on; `text` is the rendered form, which is *not* a safe key: a
 * message that literally ends in the count suffix would collide with a
 * different message that repeated that many times (messages embed
 * user-controlled paths, so a file named `game (×2).iso` is enough to
 * construct that pair).
 *
 * @param {Iterable<string>|null|undefined} messages
 * @returns {SummarizedMessage[]} one entry per distinct message, first-seen order
 */
export function summarizeMessages(messages) {
  const order = [];
  // Null-prototype object rather than a Map: this is a throwaway local, and
  // the svelte-eslint `prefer-svelte-reactivity` rule flags raw Map/Set usage
  // even where reactivity isn't wanted (same workaround as jobs.svelte.js).
  const counts = Object.create(null);
  for (const raw of messages ?? []) {
    const key = typeof raw === 'string' ? raw : String(raw);
    if (counts[key] === undefined) {
      counts[key] = 0;
      order.push(key);
    }
    counts[key] += 1;
  }
  return order.map((key) => ({
    key,
    text: counts[key] > 1 ? `${key} (×${counts[key]})` : key,
    count: counts[key],
  }));
}
