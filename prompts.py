# prompts.py — brand-agnostic prompt assembly.
#
# The agent has NO built-in business of its own. Every brand supplies its own
# outbound_prompt, inbound_prompt, business_context, and greeting via the Brands
# editor. When a brand leaves a field blank, a neutral generic fallback is used
# (GENERIC_OUTBOUND_PROMPT / GENERIC_INBOUND_PROMPT and no hardcoded facts) — never
# any particular brand's script, locations, or pricing.

BUSINESS_CONTEXT_HEADING = "━━━ AUTHORITATIVE BUSINESS FACTS ━━━"
CALL_CONTROL_HEADING = "━━━ AUTHORITATIVE CALL ENDING RULES ━━━"
CALLER_COMMUNICATION_HEADING = "━━━ AUTHORITATIVE CALLER COMMUNICATION RULES ━━━"
TRIAL_BOOKING_HEADING = "━━━ AUTHORITATIVE BOOKING RULES ━━━"
DELIVERY_STYLE_HEADING = "━━━ AUTHORITATIVE DELIVERY & PACING RULES ━━━"
CALLER_CONTEXT_HEADING = "━━━ CALLER CONTEXT ━━━"


def _attach_caller_context(prompt: str, caller_phone: str = "") -> str:
    """Tell the agent the caller's phone number — known for every call (we dialed it
    outbound; it comes from the SIP attributes inbound) — so it uses it directly for
    bookings/SMS instead of asking the caller to read it out."""
    caller_phone = (caller_phone or "").strip()
    if not caller_phone or CALLER_CONTEXT_HEADING in prompt:
        return prompt
    return (
        prompt.rstrip()
        + "\n\n"
        + CALLER_CONTEXT_HEADING
        + f"\nYou already know the caller's phone number: {caller_phone}. Use it directly as the "
          "phone argument for book_appointment and send_sms_confirmation — do NOT ask the caller to "
          "read out their number. Only ask for a number if the caller explicitly wants the "
          "confirmation sent to a different one."
    )


def _attach_delivery_rules(prompt: str) -> str:
    """Universal voice-delivery guidance attached to EVERY prompt, so every brand —
    generated, hand-written, or default — speaks calmly, pauses, and takes turns,
    regardless of what its own script says."""
    if DELIVERY_STYLE_HEADING in prompt:
        return prompt
    return (
        prompt.rstrip()
        + "\n\n"
        + DELIVERY_STYLE_HEADING
        + "\nSpeak in a warm, calm, unhurried voice — a little slower than normal. Never rush or run "
          "sentences together. Keep each turn to one or two short sentences; speak longer only when the "
          "caller asks you to explain something. After you ask a question, STOP and wait for the caller "
          "to reply — never answer for them or keep talking. Leave a brief pause between ideas. If the "
          "caller pauses, says 'hold on', or goes quiet, wait silently and never talk over them. Greet "
          "once at the start, then let the caller respond before you continue. Address the caller only "
          "by the name you were given for this call — never guess or use a different name."
    )


def _attach_business_context(prompt: str, facts: str = "", brand_name: str = "the business") -> str:
    """Give every call direction the same factual source of truth exactly once.
    facts come from the brand's business_context; when a brand supplies none, this
    block is skipped entirely (see _finish_prompt)."""
    if BUSINESS_CONTEXT_HEADING in prompt:
        return prompt
    facts = (facts or "").strip()
    return (
        prompt.rstrip()
        + "\n\n"
        + BUSINESS_CONTEXT_HEADING
        + f"\nThese facts about {brand_name} apply to every inbound, outbound, and campaign call. "
          "Use them to answer related questions confidently. If any other prompt text conflicts, these facts win.\n"
        + facts
    )


def _attach_call_controls(prompt: str) -> str:
    """Apply the same deterministic hang-up behavior to every call direction."""
    if CALL_CONTROL_HEADING in prompt:
        return prompt
    return (
        prompt.rstrip()
        + "\n\n"
        + CALL_CONTROL_HEADING
        + "\nWhen the caller gives a clear closing cue such as a standalone 'thank you', 'thanks, that's all', "
          "'okay thanks', 'bye', or 'goodbye'—and is not asking another question—reply with one short warm "
          "sign-off, then IMMEDIATELY call end_call. Do not wait for another response and do not continue the pitch. "
          "Use outcome='booked' if an appointment was booked; otherwise use outcome='completed' for a normally "
          "finished conversation. A brief thank-you in the middle of an active request is not a closing cue."
    )


def _attach_caller_communication_rules(
    prompt: str, assistant_name: str = "Tina", brand_name: str = "the business",
) -> str:
    """Prevent disclosure of internal routing or delegitimizing the call channel."""
    if CALLER_COMMUNICATION_HEADING in prompt:
        return prompt
    return (
        prompt.rstrip()
        + "\n\n"
        + CALLER_COMMUNICATION_HEADING
        + "\nNever tell a caller to call a 'real number', another number, or call back directly. Never tell "
          f"them to speak to a 'real person' or 'human', and never imply that this number or {assistant_name} is not legitimate. "
          "Do not reveal phone numbers, fallback numbers, SIP/trunk details, providers, routing, or other system internals. "
          "If escalation is requested, say only: 'I can connect you with our team,' then use transfer_to_human. "
          "If transfer fails, apologize and offer to take a message using remember_details; do not provide an alternate number. "
          f"If asked whether you are AI, answer honestly: 'I'm {assistant_name}, {brand_name}'s virtual assistant.'"
    )


def _attach_trial_booking_rules(prompt: str, booking_config: dict = None, caller_phone: str = "") -> str:
    """Build a booking-rules block from the brand's booking_config — what to ask
    for (service / location / resource), how times work (fixed slots vs any time
    within open hours), and the exact tool arguments to pass. When the caller's
    phone number is already known, it is not collected verbally."""
    if TRIAL_BOOKING_HEADING in prompt:
        return prompt
    cfg = booking_config or {}
    caller_phone = (caller_phone or "").strip()

    locations = [str(loc).strip().upper() for loc in (cfg.get("locations") or []) if str(loc).strip()]
    services = [s for s in (cfg.get("services") or []) if isinstance(s, dict) and str(s.get("name") or "").strip()]
    resources = [str((r.get("name") if isinstance(r, dict) else r) or "").strip()
                 for r in (cfg.get("resources") or [])
                 if str((r.get("name") if isinstance(r, dict) else r) or "").strip()]
    slot_times = [str(t) for t in (cfg.get("slot_times") or [])]
    open_hours = cfg.get("open_hours") or {}
    slot_mode = str(cfg.get("slot_mode") or "").strip().lower()
    if slot_mode not in ("fixed", "open_hours"):
        slot_mode = "open_hours" if (open_hours and not slot_times) else "fixed"
    try:
        default_duration = int(cfg.get("duration_minutes") or 60)
    except (TypeError, ValueError):
        default_duration = 60
    raw_cap = cfg.get("capacity_per_slot")
    try:
        capacity = 1 if raw_cap in (None, "") else max(0, int(raw_cap))
    except (TypeError, ValueError):
        capacity = 1

    # The caller's number is already known on every call, so only collect it verbally
    # when (unusually) it is missing.
    collect = ["caller name"] if caller_phone else ["caller name", "phone number"]
    rules, slot_args = [], ["date", "time"]
    if caller_phone:
        rules.append(f"Use the caller's known number {caller_phone} as the phone argument — do not ask for it.")

    # Service
    if len(services) >= 2:
        svc_list = ", ".join(f"{s['name']} ({int(s.get('duration_minutes') or default_duration)} min)" for s in services)
        rules.append(f"Ask which service the caller wants — options: {svc_list}.")
        collect.insert(0, "service")
        slot_args.append("service")
    elif len(services) == 1:
        s = services[0]
        rules.append(f"The service is {s['name']} ({int(s.get('duration_minutes') or default_duration)} minutes).")

    # Time model
    if slot_mode == "open_hours" and open_hours.get("start") and open_hours.get("end"):
        try:
            slot_interval = int(cfg.get("slot_interval_minutes") or 30)
        except (TypeError, ValueError):
            slot_interval = 30
        rules.append(
            f"Bookings run between {open_hours['start']} and {open_hours['end']}, in {slot_interval}-minute "
            f"slots. The caller can book one slot or several back-to-back slots. To book more than one, collect "
            f"the start AND end time and pass both (set end_time on check_availability and book_appointment); "
            f"the total length must be a whole multiple of {slot_interval} minutes. If the caller asks for a "
            f"length that is not a multiple of {slot_interval} minutes, explain bookings are in "
            f"{slot_interval}-minute blocks and ask them to pick a start/end that fits."
        )
    elif slot_times:
        rules.append("Offer these start times: " + ", ".join(slot_times) + ".")

    # Location
    if len(locations) == 1:
        rules.append(f"All bookings are at {locations[0]} — do NOT ask the caller which location.")
    elif len(locations) >= 2:
        rules.append(f"Ask which location: {' or '.join(locations)}.")
        collect.append("location")
        slot_args.append("location")
    # (no locations → don't ask for one)

    # Resource (named, pick-a-person)
    if resources:
        rules.append(f"Ask who/which they'd like — options: {', '.join(resources)} — or offer the first available.")
        collect.append("the preferred person/court/room")
        slot_args.append("resource")
    elif capacity == 0:
        rules.append("Several bookings can share the same time (no fixed cap) — just confirm availability.")
    elif capacity > 1:
        rules.append(f"Up to {capacity} bookings are allowed at the same time; the tool reports if a slot is full.")

    check_args = ", ".join(slot_args)
    book_args = ", ".join(["name", "phone"] + slot_args)
    collect_sentence = "Collect and verbally confirm: " + ", ".join(collect + ["date", "and start time"]) + "."

    return (
        prompt.rstrip()
        + "\n\n"
        + TRIAL_BOOKING_HEADING
        + "\n" + " ".join(rules)
        + f" {collect_sentence} ALWAYS call check_availability({check_args}) first. If unavailable, present the "
          "returned alternatives and wait for the caller to choose; never book an alternative without confirmation. "
          f"Only then call book_appointment({book_args}). Never claim a booking is confirmed unless the tool returns "
          "'BOOKING CONFIRMED'."
    )


def _fill_prompt_placeholders(
    template: str,
    lead_name: str,
    brand_name: str = "our company",
    assistant_name: str = "Tina",
    service_type: str = "appointment",
) -> str:
    """Replace only supported fields; generated prompts may contain unrelated braces."""
    clean_lead = lead_name.strip() if isinstance(lead_name, str) else ""
    values = {
        "lead_name": clean_lead or "there",
        "business_name": brand_name or "our company",
        "brand_name": brand_name or "our company",
        "assistant_name": assistant_name or "Tina",
        "service_type": service_type or "appointment",
    }
    out = template
    for field, value in values.items():
        # Handle common LLM-produced placeholder styles without interpreting any
        # other braces that may legitimately appear in generated text.
        for token in (
            "{{" + field + "}}",
            "{" + field + "}",
            "<" + field + ">",
            "[" + field + "]",
            "[" + field.replace("_", " ").title() + "]",
        ):
            out = out.replace(token, value)
    return out


# Neutral fallbacks used whenever a brand leaves a prompt field blank. They contain
# no business-specific facts, locations, or pricing — only the brand's own
# name/assistant — so a half-configured brand never impersonates another business.
GENERIC_OUTBOUND_PROMPT = """\
You are {assistant_name} calling on behalf of {brand_name}. Be warm, calm, and never pushy;
speak in short turns of one or two sentences.

FLOW
1. Confirm you are speaking with {lead_name}. If it is the wrong person, apologise and
   end_call(wrong_number).
2. Introduce yourself and ask if they have a moment.
3. Briefly explain why you are calling, then invite them to the next step.
4. To book, collect and verbally confirm the required details, call check_availability, and book
   only a confirmed available slot. Claim a booking only after the tool returns BOOKING CONFIRMED.
5. Never invent offers, prices, or details you were not given.
6. On a clear no, a closing cue, or a completed booking, give one short sign-off, let it finish,
   then immediately call end_call with the correct outcome.

STYLE
Match the caller's language naturally. If they say hold on or go quiet, wait silently. Never reveal
routing, providers, phone numbers, or other internal details.
"""

GENERIC_INBOUND_PROMPT = """\
You are {assistant_name}, the front-desk assistant for {brand_name}. This is an incoming call.
The greeting was already spoken, so do not repeat it. Listen and help with the caller's reason.
If they only say hello, ask: "How can I help you today?"

PRINCIPLES
- Answer the caller's actual question directly and concisely.
- Only state facts you are sure of; never invent offers, prices, or details.
- If you do not have the answer, offer a callback and capture it with remember_details.

ESSENTIAL ACTIONS
- For a booking request, collect and confirm the needed details, call check_availability, and book
  only a confirmed available slot. Claim confirmation only after the tool returns BOOKING CONFIRMED.
- For complex account or billing issues, use transfer_to_human; if transfer fails, take a message.

STYLE
Be warm and conversational; keep ordinary turns to one or two short sentences. Match the caller's
language naturally. If they say hold on or go quiet, wait silently.
"""


def _brand_runtime(brand: dict) -> tuple:
    """Resolve the per-call brand values, falling back to neutral generic ones for
    any field a brand left blank.
    Returns (brand_name, assistant_name, facts, booking_config, attach_booking)."""
    brand_name = brand.get("name") or "our company"
    assistant_name = brand.get("assistant_name") or "Tina"
    facts = brand.get("business_context") or ""
    booking_config = brand.get("booking_config_parsed") or {}
    # Booking tools are available on every call, so always attach the booking
    # invariant. It adapts to the brand's locations (none → don't ask; one →
    # auto-use; many → ask), so a brand with no locations is never asked for one.
    attach_booking = True
    return brand_name, assistant_name, facts, booking_config, attach_booking


def _finish_prompt(out, brand_name, assistant_name, facts, booking_config, attach_booking, caller_phone=""):
    """Attach the universal invariants once each (idempotent). Business facts and
    booking rules are skipped when a brand supplies none, so a brand with no facts
    never gets another business's facts or locations bolted on."""
    out = _attach_delivery_rules(out)
    out = _attach_call_controls(out)
    if facts and facts.strip():
        out = _attach_business_context(out, facts, brand_name)
    out = _attach_caller_communication_rules(out, assistant_name, brand_name)
    out = _attach_caller_context(out, caller_phone)
    if attach_booking:
        out = _attach_trial_booking_rules(out, booking_config, caller_phone)
    return out


def build_prompt(
    lead_name: str = "there",
    brand: dict = None,
    custom_prompt: str = None,
    caller_phone: str = "",
) -> str:
    """Build the outbound system prompt for a brand.

    Base text is the call-specific custom/campaign prompt, else the brand's
    outbound_prompt, else the neutral generic script. caller_phone is the number
    we dialed, surfaced to the agent so it never asks the caller to recite it."""
    brand = brand or {}
    brand_name, assistant_name, facts, booking_config, attach_booking = _brand_runtime(brand)
    base = custom_prompt or brand.get("outbound_prompt") or GENERIC_OUTBOUND_PROMPT
    out = _fill_prompt_placeholders(base, lead_name, brand_name, assistant_name)
    return _finish_prompt(out, brand_name, assistant_name, facts, booking_config, attach_booking, caller_phone)


def build_inbound_prompt(
    lead_name: str = "there",
    brand: dict = None,
    campaign_catalog: str = "",
    caller_phone: str = "",
) -> str:
    """Build the inbound system prompt for a brand, with a small active-campaign
    index. Base text is the brand's inbound_prompt, else the neutral generic script.
    caller_phone is the caller's number (from the SIP attributes), surfaced to the
    agent so it never asks the caller to recite it."""
    brand = brand or {}
    brand_name, assistant_name, facts, booking_config, attach_booking = _brand_runtime(brand)
    base = brand.get("inbound_prompt") or GENERIC_INBOUND_PROMPT
    out = _fill_prompt_placeholders(base, lead_name, brand_name, assistant_name)
    if campaign_catalog and campaign_catalog.strip():
        out += ("\n\nACTIVE CAMPAIGN INDEX (recognition only; retrieve details before answering)\n"
                + campaign_catalog.strip())
    return _finish_prompt(out, brand_name, assistant_name, facts, booking_config, attach_booking, caller_phone)


def build_greeting(lead_name: str = "there", brand: dict = None, is_inbound: bool = False) -> str:
    """The opening line spoken aloud on pickup (via TTS). Uses the brand's own
    `greeting` field (with {lead_name}/{assistant_name}/{brand_name} filled in) when set;
    otherwise a sensible default. Only inbound calls speak a separate greeting; the
    system prompt is separately told this was already spoken, so the model never repeats it."""
    brand = brand or {}
    assistant_name = brand.get("assistant_name") or "Tina"
    brand_name = brand.get("name") or "our company"
    raw = (brand.get("greeting") or "").strip()
    if raw:
        return _fill_prompt_placeholders(raw, lead_name, brand_name, assistant_name)
    if is_inbound:
        return f"Hi, this is {assistant_name} from {brand_name}. How can I help you?"
    clean_lead = (lead_name or "").strip() if isinstance(lead_name, str) else ""
    who = clean_lead if (clean_lead and clean_lead != "there") else ""
    return f"Hi, am I speaking with {who}?" if who else f"Hi, this is {assistant_name} from {brand_name}."


# ── Campaign prompt generation & assembly ───────────────────────────────────────

# Instruction template sent to the LLM to turn a campaign's plain-English purpose
# (+ accumulated feedback) into a complete outbound call script and a short summary.
CAMPAIGN_GEN_INSTRUCTIONS = """You are a prompt engineer for an outbound voice AI agent.
Using the inputs below, write (1) a COMPLETE outbound call script for this one campaign, and
(2) a short summary of what the campaign offers.

── Reference: the brand's default outbound script (match this style & tool usage) ──
{default_base}

── Campaign name ──
{name}

── Campaign purpose (what this campaign is about) ──
{purpose}

── Improvement feedback to incorporate (oldest first; honour the latest) ──
{feedback}

Rules for the call script:
- The agent is warm, calm, speaks at a relaxed slightly-slower pace, short turns (1-2 sentences).
- Open by confirming identity, then pursue THIS campaign's goal naturally.
- Use tools where relevant: check_availability, book_appointment, send_sms_confirmation,
  transfer_to_human, remember_details, and ALWAYS end_call with a warm sign-off (never hang up abruptly).
- Stay focused on this campaign's purpose; keep it self-contained.

Return ONLY valid JSON, no markdown, with exactly these keys:
{{"prompt": "<the full outbound call script>", "summary": "<2-4 line plain-English summary of the offer for other agents to reference>"}}
"""


# Turns a plain-English description of a business into the four editable brand
# fields. The agent name and brand name are supplied so the generated text refers
# to them correctly. Generic hang-up / disclosure / booking-confirmation rules are
# attached automatically at call time, so the generated prompts must NOT restate
# them — they should focus on persona, the offer, the conversation flow, and facts.
BRAND_GEN_INSTRUCTIONS = """You are an expert prompt engineer for a REAL-TIME VOICE phone agent. From a
plain-English description of a business, write the agent's prompts. The agent is named "{assistant_name}"
and the business is "{brand_name}".

── Business description ──
{description}

GLOBAL RULES (apply to both scripts):
- Names: when the script addresses the person on the call, write the literal token {{lead_name}} — it is
  replaced with the real name at call time. NEVER invent, assume, or hard-code a specific person's name
  (no "Priya", "Rahul", etc.). For the agent and business, use {assistant_name} and {brand_name}.
- Voice delivery (this is spoken aloud, not read): warm, calm, unhurried, a little SLOWER than normal;
  never rush or run sentences together. One or two SHORT sentences per turn. After asking a question,
  STOP and wait for the answer — do not keep talking or answer for them. Pause between ideas; never talk
  over the caller.
- Facts: use only what's in the description; never invent prices, offers, addresses, or numbers.

Produce FOUR things:

1. business_context — the authoritative FACTS as concise bullet points (what the business does,
   services/offers, hours, pricing, locations). Only what is in or clearly implied by the description.

2. outbound_prompt — the script for calls the agent MAKES, written as a NUMBERED step-by-step flow:
   1) OPEN: greet and confirm identity — e.g. "Hi, am I speaking with {{lead_name}}?". Briefly handle
      wrong person / voicemail / no-answer.
   2) PERMISSION: introduce yourself as {assistant_name} from {brand_name} and ask if it's a good moment.
   3) PITCH: explain the offer in two or three short sentences — value, not a monologue.
   4) INVITE: propose the next step (e.g. a booking) and ask one easy question.
   5) OBJECTIONS: list 2-3 objections likely for THIS business, each with a short warm reply.
   6) CLOSE: confirm the next step in one or two sentences.
   Keep every turn short and pause for the caller's replies.

3. inbound_prompt — how {assistant_name} answers INCOMING calls as the front desk for {brand_name}. The
   greeting is already spoken, so do not repeat it. Do NOT assume the caller's name — ask only if needed.
   Handle the caller's reason first, answer using the facts, help with bookings, escalate complex issues.

4. greeting — INBOUND ONLY: the single opening line {assistant_name} says out loud the moment an INCOMING
   call connects, before anything else. One short, warm sentence with {assistant_name} and {brand_name}
   (e.g. "Hi, this is {assistant_name} from {brand_name} — how can I help?"). Outbound calls do NOT use this
   line; they open with step 1 of the outbound_prompt instead, so do not write an outbound-style opener here.

Do NOT restate generic rules about hanging up, revealing internal details, pacing, or confirming bookings
only after the tool succeeds — those are added automatically. Keep each script focused, not bloated.

Return ONLY valid JSON, no markdown, with exactly these keys:
{{"business_context": "<facts>", "outbound_prompt": "<outbound script>", "inbound_prompt": "<inbound script>", "greeting": "<one-line opening greeting>"}}
"""


# Applies plain-English feedback to a brand's EXISTING prompts. The model must
# change only what the feedback asks for and return the full text of all three
# fields (unchanged ones returned as-is), so the editor can drop them straight in.
BRAND_REFINE_INSTRUCTIONS = """You are refining the prompts for a voice AI phone agent named
"{assistant_name}" for "{brand_name}". Apply the user's feedback to the CURRENT prompts below.
Change ONLY what the feedback asks for and preserve everything else. Keep the literal token {{lead_name}}
wherever the person's name appears and NEVER replace it with a specific invented name. Do NOT add generic
rules about hanging up, revealing internal details, pacing, or confirming bookings — those are added
automatically.

── Feedback to apply ──
{feedback}

── Current business facts ──
{business_context}

── Current outbound prompt ──
{outbound_prompt}

── Current inbound prompt ──
{inbound_prompt}

── Current greeting (spoken first) ──
{greeting}

Return the FULL revised text for every field (return a field unchanged if the feedback doesn't touch it).
Return ONLY valid JSON, no markdown, with exactly these keys:
{{"business_context": "<facts>", "outbound_prompt": "<outbound script>", "inbound_prompt": "<inbound script>", "greeting": "<one-line opening greeting>"}}
"""


# Brand-neutral campaign flow. The brand's facts, booking rules, and identity are
# attached downstream by build_prompt(), so this core must not hardcode any one
# brand's locations, pricing, or name — otherwise they would leak across brands.
GENERIC_CAMPAIGN_CORE = """\
You are {assistant_name} calling on behalf of {brand_name}. Be warm, calm, and never pushy;
speak at a relaxed, slightly-slower pace in short turns (one or two sentences).

FLOW
1. Confirm you are speaking with {lead_name}. If it is the wrong person, apologise and
   end_call(wrong_number).
2. Introduce yourself and ask if they have a moment.
3. Explain the campaign offer below in at most three short sentences, then invite them to the
   next step.
4. To book, collect and verbally confirm the required details, call check_availability, and book
   only a confirmed available slot. Claim a booking only after the tool returns BOOKING CONFIRMED.
5. For details not covered here, call lookup_campaign. Never invent offer details.
6. On a clear no, a closing cue, or a completed booking, give one short sign-off, let it finish,
   then immediately call end_call with the correct outcome.

STYLE
Match the caller's language naturally. If they say hold on or go quiet, wait silently. Never reveal
routing, providers, phone numbers, or other internal details.
"""


def assemble_outbound_prompt(
    campaign_name: str,
    campaign_summary: str = "",
    campaign_purpose: str = "",
) -> str:
    """Build a bounded, brand-neutral campaign prompt; the brand's facts/booking
    rules are attached later by build_prompt(). Other campaigns are retrieved on
    demand via lookup_campaign."""
    name = " ".join((campaign_name or "Campaign").split())[:120]
    details = " ".join((campaign_summary or campaign_purpose or "").split())[:900]
    campaign = (
        "COMPACT_CALL_PROMPT\n"
        f"PRIMARY CAMPAIGN: {name}\n"
        f"Use this as the call's focus: {details or 'Introduce the campaign briefly and invite them to the next step.'}\n"
        "Do not proactively discuss other campaigns. Use lookup_campaign only if the lead asks.\n\n"
    )
    return campaign + GENERIC_CAMPAIGN_CORE
