# Design rationale

Why each significant decision was made, and what the alternative was. If you
want to challenge a choice in this repository, this is the file to argue with.

## Why WooCommerce, out of the four options

Freshdesk, Zoho Inventory and Unicommerce are all cloud-only, so a reviewer
cannot verify the connector without obtaining an account. Freshdesk in
particular rejects free email providers at signup, which makes an honest live
run impossible for a candidate. WooCommerce is self-hostable, so the entire
system can be stood up and verified on a laptop with no vendor relationship.
That is why `deploy/woo-local` exists and why continuous integration runs the
connector against real WordPress and WooCommerce rather than only a double.

**Tradeoff accepted:** WooCommerce has no multi-tenant SaaS story, so the
hosted-mode concerns that a Freshdesk connector would force are absent here.
They are listed as unbuilt rather than pretended.

## Why read-only, and why the credential enforces it

Every tool is annotated `readOnlyHint`, and the client only issues GET. That
is a convention, and conventions break. WooCommerce scopes each API key to
Read, Write or Read-Write at creation, so asking for a Read key moves the
guarantee below the code: the store returns `401 The API key provided does not
have write permissions` for any write, whatever this repository does.
`scripts/assert_read_only.py` proves it by signing a write by hand, bypassing
the connector entirely.

The first version of that script was wrong in an instructive way: it asserted
only that the write was refused. An invalid key is refused for everything, so
it passed for a stale credential and announced a guarantee that did not hold.
A negative test that cannot fail for the right reason is worse than no test,
because it manufactures confidence. It now requires a successful read first,
and distinguishes "refused for lack of write permission" from "refused
because the credential is invalid", which are both `401`.

**Alternative rejected:** a Read-Write key with write tools behind approval.
That is the right end state, but it changes the security conversation with the
merchant and belongs in a second phase, not a take-home.

## Why OAuth 1.0a is implemented when an API key would do

`WC_REST_Authentication::authenticate()` attempts Basic auth only when
`is_ssl()` is true and otherwise falls through to OAuth. A plain-HTTP store
therefore treats a key and secret in the query string as anonymous, and every
call fails with `woocommerce_rest_cannot_view`, which reads like a roles
problem rather than an auth one. I hit exactly that against the local store,
read the plugin source, and implemented signing to match.

The signature construction copies WooCommerce's own, including two places it
departs from the specification: each `key=value` pair is RFC 3986 encoded as a
whole string and the pairs joined with `%26`, and the signing key is
`consumer_secret + "&"` with no token secret. The mock store verifies
signatures independently, so the tests cannot pass by agreeing with
themselves.

## Why the rate limiter is client-side

WooCommerce core ships no rate limiter for the REST API. Throttling comes from
the host, from the Store API if a merchant enabled it, or not at all. The
dangerous case is the last one: an agent paging through orders takes down a
shared-hosting store. So the budget is enforced here regardless, with a
reserve left for the merchant's other integrations, and raised only when the
store advertises a real limit through `RateLimit-*` headers.

**Known limit:** the budget is in-process, so several replicas would each
think they had the whole allowance. The interface is shaped so a shared
backend is a constructor change.

## Why the model never writes query syntax

Tools take typed, allow-listed arguments and `query.py` turns them into
`wc/v3` parameters. A prompt-injected "search for `?role=administrator`" is
rejected before a request exists. It also means a bad argument fails with a
fixable message instead of an opaque 400 from the store.

## Why signals are rule-based rather than model-extracted

Everything in `insights.py` runs in process with no network. Three reasons.
Customer text never reaches a third-party model, which is the first question a
merchant's security review asks. The same order always produces the same
signals, so the eval scores are comparable across changes. And every intent
carries `intent_evidence`, the literal field that triggered it, so a merchant
can audit a routing decision rather than trust it.

**Cost accepted:** rules miss phrasings a model would catch. The mitigation is
the false-positive corpus in `tests/test_signals.py`: every new pattern must
ship with a negative case, because a wrong payment reference means a customer
is told something false about their money.

## Why reconciliation is the headline, not payment-reference extraction

Extracting Razorpay ids is table stakes once you know they are there. The
insight is that **a WooCommerce refund row is not evidence that money moved.**
WooCommerce itself says so, in a note on the order:

> Order status set to refunded. To return funds to the customer you will need
> to issue a refund through your payment gateway.

The order still reads `refunded` with a net payment of zero, so the warning is
easy to miss and the agent would confidently tell the customer their money is
on the way. `signals.reconciliation` surfaces the gap from the store side, and
the Razorpay connector resolves it.

## Why there are two connectors rather than one

The store knows what it believes. The gateway knows what happened. Neither can
answer "where is my refund?" alone, and merging them into one server would
hide that. Two MCP servers composed by the agent is also how Agent Studio
actually works, so the demo shows the real shape rather than a convenient one.

`verify_refund` returns one of five verdicts rather than a boolean, because
`confirmed`, `in_flight`, `failed` and `never_issued` need four different
things said to the customer. Each verdict carries a `customer_safe_message`
phrased not to promise money that has not moved.

## Why amounts are converted in exactly one place

Razorpay returns paise; WooCommerce returns rupees as decimal strings. A
factor-of-100 error is invisible in a demo and expensive in production, so
`paise_to_major` is the single boundary and it is unit tested.

## Why the evals are deterministic

Scoring is string and regex matching, not a model judge, so a change in the
score means a change in the connector rather than variance in a grader. Oracle
mode runs the reference calls with no model at all, which is what lets the
dataset gate continuous integration without an API key.

The safety cases assert on what must **not** appear in an answer. The
expensive failure is not a wrong order number; it is the phrase "your refund
has been sent" attached to money that never moved.

## What I know is unverified, and why that matters to me

The WooCommerce half is verified against a real store, including two findings
I would have got wrong from documentation alone: Basic auth silently failing
without TLS, and WooCommerce's own order note admitting a refund row does not
move money.

The Razorpay half is not. It was built against a mock reproducing the
published contract, which is exactly the position I was in with WooCommerce
before a real store corrected me. I am not going to claim the two are
equivalent. `docs/CAPABILITIES.md` has the component-by-component status, and
the base URL is configurable precisely so that gap closes with credentials
rather than a rewrite.

## What I would change with more time

1. A shared rate budget, the moment there is more than one replica.
2. Multi-tenancy: tenant resolved from a hashed bearer token, never from a
   tool argument.
3. WooCommerce webhooks into an Agent Studio trigger, so the agent drafts
   before a human opens the order.
4. Write tools behind human approval, which requires a Read-Write key and so
   is a conversation with the merchant rather than a flag.
