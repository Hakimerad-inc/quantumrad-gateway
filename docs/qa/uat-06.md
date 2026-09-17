# S06-T10: Clean-VM UAT (PRD §9 Phase 1, K4)

**Date:** _fill in_
**Tester:** _fill in_
**Time elapsed:** _fill in (target ≤10 min)_
**Gateway version:** _fill in_

## Prerequisites

- [ ] Clean Windows VM with no prior gateway installation
- [ ] Installer available (from S06-T9)
- [ ] Timer ready

## Walkthrough

### 1. Install (2 min)

| Step | Expected | Result |
|------|----------|--------|
| Run installer | Dialog appears; no UAC errors | ☐ |
| Choose install path | Default path accepted | ☐ |
| Finish install | Gateway icon on desktop; service not started | ☐ |
| Size check | Installer ≤250 MB (K6) | ☐ |

### 2. First run — web wizard (4 min)

| Step | Expected | Result |
|------|----------|--------|
| Launch gateway | `mercure-gateway --web` starts; console shows receiver/forwarder | ☐ |
| Open browser → `http://localhost:8080` | Dashboard loads; receiver + forwarder "running" | ☐ |
| Click **Setup** | Wizard step 1 shows default AE Title `GATEWAY`, port 11112 | ☐ |
| Click **Next →** | Wizard advances to Destinations | ☐ |
| Leave destinations empty → click **Next →** | Validation error: "at least one destination required" | ☐ |
| Add a destination | Fields appear for Name, Host, Port, AET | ☐ |
| Click **Echo** | Status badge shows "ok" (if a real PACS is reachable) or "refused" | ☐ |
| Click **Next →** | Wizard advances to Reports | ☐ |
| Leave reports disabled → click **Next →** | Wizard advances to Summary | ☐ |
| Click **Save Configuration** | "Setup Complete" shown; config persisted | ☐ |

### 3. Receive a study (2 min)

| Step | Expected | Result |
|------|----------|--------|
| Send a DICOM study to `GATEWAY@localhost:11112` | Receiver logs the C-STORE | ☐ |
| Open Queue tab | Study appears in the paginated table | ☐ |

### 4. Admin panel (2 min)

| Step | Expected | Result |
|------|----------|--------|
| Click **Config** tab | Editable JSON config displays | ☐ |
| Open **Logs** tab | Operations log tail visible | ☐ |
| Open **Audit** tab | Audit events from the receive + setup visible | ☐ |
| Click **Verify Chain Integrity** | Badge shows "✓ Chain intact" | ☐ |

## Final

- [ ] Total time ≤10 min (K4)
- [ ] No critical bugs found
- [ ] Phase 1 exit criteria met (PRD §9)

**Notes:** _(fill in any issues, observations, or improvements)_
