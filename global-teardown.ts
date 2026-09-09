/** Playwright globalTeardown: gracefully stop the gateway (SIGTERM; main()
 * installs a graceful-shutdown handler so the spool marker gets written). */

import { stopGateway } from "./e2e/tests/utils";

async function globalTeardown() {
  await stopGateway();
}

export default globalTeardown;
