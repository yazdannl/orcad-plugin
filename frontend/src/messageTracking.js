export function responseMatches(message, expected, includeSeq = false) {
  if (!message || !expected) return false
  if (message.request_id !== expected.requestId || message.revision_id !== expected.revisionId) return false
  return !includeSeq || message.seq === expected.seq
}
