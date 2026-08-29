"""Pure character-side core: the ``CharacterState`` hand-off type and order-slot math.

Deterministic. Consumed by the ``market`` core and the pipeline; never touches the
I/O shell. (The opportunity-ranking engine that once lived here was removed with the
market-trade advisor; see ARCHITECTURE.md § Current state.)
"""
