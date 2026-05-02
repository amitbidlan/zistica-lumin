// Redirect handled at the config level (next.config.mjs `redirects()`)
// so it produces a real HTTP Location header for all clients.
// This page is only reached if the config redirect is bypassed.
export default function Home() {
  return null;
}
