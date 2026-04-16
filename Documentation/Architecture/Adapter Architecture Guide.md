# MBB ya Kin: Universal Adapter Architecture Guide
**"Structure as a Service" - Build Once, Scale Infinitely**

---

## 1. Vision & Purpose
To ensure **MBB ya Kin** is resilient to DRC infrastructure challenges and scalable for Phase 2+, we adopt the **Adapter Pattern**. This decouples the "Conversation Brain" from specific tools like Airtable, Claude, or Orange Money.

**Primary Goal:** Switch backends (CRM, Inventory, AI, Payments) via `.env` configuration with **zero code changes** to the core bot logic.

---

## 2. Core Architecture: The "Universal Socket"
The bot communicates through an **Interface Contract**. The **Active Adapter** translates these requests for the specific provider.

`[ Conversation Engine (The Brain) ]`  
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; &darr;  
`[ Generic Interface (The Socket) ]`  
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; &darr;  
`[ Specific Adapter (Airtable / Claude / Orange Money) ]`

---

## 3. Integration Catalog (Phased Roadmap)

### A. Intelligence (AI Models)
| Env Variable (`AI_ADAPTER`) | Provider | Phase | DRC Strategic Use |
| :--- | :--- | :--- | :--- |
| `ANTHROPIC_CLAUDE` | Claude 3.5 | Phase 1 | High-quality lead reasoning & multi-language detection. |
| `GOOGLE_GEMINI` | Gemini 1.5 | Phase 1/2 | Backup for high volumes; better edge performance in Africa. |
| `LOCAL_LLAMA` | Llama 3 | Phase 3 | Edge computing for privacy or during complete internet outages. |

### B. CRM & Lead Management 
| Env Variable (`CRM_ADAPTER`) | Provider | Phase | DRC Strategic Use |
| :--- | :--- | :--- | :--- |
| `AIRTABLE` | Airtable | Phase 1 | Immediate launch; non-technical dashboard for staff. |
| `MBB_HUB` | Internal CRM | Phase 2 | Full synchronization with the centralized MBB ecosystem. |
| `LOCAL_MOCK` | Mock Database | Dev | Testing without internet or active API. |

### C. Inventory & Products
| Env Variable (`INVENTORY_ADAPTER`) | Provider | Phase | DRC Strategic Use |
| :--- | :--- | :--- | :--- |
| `STATIC_JSON` | Local File | Phase 1 | Works without API calls; ultra-fast response for product lists. |
| `MBB_BOX` | Internal API | Phase 2+ | Real-time stock verification for Kinshasa warehouses. |

### D. Payment Gateways
| Env Variable (`PAYMENT_ADAPTER`) | Provider | Phase | DRC Strategic Use |
| :--- | :--- | :--- | :--- |
| `ORANGE_MONEY` | Orange CD | Phase 1 | Dominant mobile money in Kinshasa. |
| `AIRTEL_MONEY` | Airtel CD | Phase 1 | Strong in Eastern DRC. |
| `MBB_PAYMENTS` | Internal API | Phase 2 | Unified financial reconciliation across MBB. |

### E. Messaging Channels
| Env Variable (`MESSAGING_ADAPTER`) | Provider | Phase | DRC Strategic Use |
| :--- | :--- | :--- | :--- |
| `WHATSAPP_API` | WhatsApp | Phase 1 | Primary user channel. |
| `SMS_FALLBACK` | Local SMS | Phase 2 | Delivery backup for weak network areas. |

### F. Growth & Digital Presence
This is intentionally **outside** the bot's adapter core.

- Social accounts, paid ads, post scheduling, and website/CMS management belong to a separate Digital Presence Platform.
- The bot consumes only the results of that platform: click-to-WhatsApp traffic, attribution metadata, captured forms, and future web chat/widget events.
- This separation keeps CRM responsibilities in `CRM_ADAPTER`, inventory in `INVENTORY_ADAPTER`, and conversation channels in `MESSAGING_ADAPTER`.

---

## 4. Developer Implementation Guide

### Step 1: Define the Interface (The Contract)
```python
from abc import ABC, abstractmethod

class AIAdapterInterface(ABC):
    @abstractmethod
    async def get_chat_completion(self, messages: list) -> str:
        """Standard method to get AI text back."""
        pass
```

### Step 2: Implement Concrete Adapters
```python
# Claude implementation
class ClaudeAdapter(AIAdapterInterface):
    async def get_chat_completion(self, messages):
        # Anthropic SDK logic here
        return "Responded via Claude"

# Gemini implementation
class GeminiAdapter(AIAdapterInterface):
    async def get_chat_completion(self, messages):
        # Gemini SDK logic here
        return "Responded via Gemini"
```

### Step 3: The Factory Pattern (Switching Logic)
```python
def get_ai_adapter():
    choice = os.getenv("AI_ADAPTER", "ANTHROPIC_CLAUDE")
    if choice == "GOOGLE_GEMINI":
        return GeminiAdapter()
    return ClaudeAdapter()
```

---

## 5. DRC Resilience Rules (Mandatory)

1.  **Fail-Fast/Fallback:** Every adapter must have a `try/except` block that falls back to a local queue (Redis) if the external API (HUB/BOX/Claude) times out.
2.  **Idempotency Keys:** Every request must include a `lead_id` or `order_id` in the header to prevent duplicates when retrying on shaky 3G/4G connections.
3.  **Payload Buffers:** Minimize output tokens for AI (max 150) to ensure the message delivers over low bandwidth.
4.  **Circuit Breaker:** If an adapter fails 5 times in a row, auto-trip the circuit and notify the "Escalation System".

---

## 6. How to Switch Model/Backend
1. Open `.env` file.
2. Update the target variable (e.g., `AI_ADAPTER=GOOGLE_GEMINI`).
3. Restart the Docker container.
4. **Done.** No logic changes needed.

---

## 7. Testing Checklist for New Adapters
- [ ] **Interface Match**: Implements all abstract methods.
- [ ] **Timeout Handling**: Returns/fails within 10 seconds.
- [ ] **3G Simulation**: Works correctly under throttled network conditions.
- [ ] **Environmental Security**: Credentials loaded via `.env` only.

