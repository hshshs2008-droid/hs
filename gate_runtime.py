"""Isolate exchange state while sharing approvals and Telegram delivery limits."""

import time
from contextlib import nullcontext
from pathlib import Path

from alerts import AlertStore
from market import MarketError


class GateStore(AlertStore):
    """Gate keeps its own signals and ledger; the main store owns membership."""

    def __init__(self, path, main_store, *, activation_started_at_ms=None):
        if (activation_started_at_ms is not None and
                (type(activation_started_at_ms) is not int or not 0 <= activation_started_at_ms < 2**63)):
            raise ValueError("Invalid Gate activation timestamp.")
        if str(path) != ":memory:":
            with main_store._lock:
                main_path = main_store._db.execute("PRAGMA database_list").fetchone()[2]
            if main_path and Path(path).resolve() == Path(main_path).resolve():
                raise ValueError("Gate state must use a separate database file.")
        self.main_store = main_store
        self.eligible_symbol = None
        super().__init__(path, exchange_name='Gate.io Spot')
        self.admin_id = main_store.admin_id
        self._access_enforced = True
        with self._lock:
            started = self._metadata("gate_activation_started_at_ms", None)
            if started is None:
                started = (int(time.time() * 1000) if activation_started_at_ms is None
                           else activation_started_at_ms)
                self._set_metadata("gate_activation_started_at_ms", started)
            if type(started) is not int or not 0 <= started < 2**63:
                raise ValueError("Invalid saved Gate activation timestamp.")
            self.activation_started_at_ms = started

    def access_required(self):
        return self.main_store.access_required()

    def can_access(self, chat_id):
        return self.main_store.can_access(chat_id)

    def is_admin(self, chat_id):
        return self.main_store.is_admin(chat_id)

    def subscribers(self):
        # Copy only current, approved subscriptions; replacing an activation
        # also discards any queued delivery from the earlier subscription.
        active = {chat_id: max(activation, self.activation_started_at_ms)
                  for chat_id, activation in self.main_store.subscribers()}
        with self._lock:
            # record() may already own a transaction containing a new signal.
            # Reconciliation must participate in it, not commit it early.
            transaction = nullcontext() if self._db.in_transaction else self._db
            with transaction:
                previous = dict(self._db.execute('SELECT chat_id, activation_ms FROM subscriptions'))
                for chat_id, activation in previous.items():
                    if active.get(chat_id) != activation:
                        self._db.execute('DELETE FROM subscriptions WHERE chat_id = ?', (chat_id,))
                for chat_id, activation in active.items():
                    if previous.get(chat_id) != activation:
                        self._db.execute('INSERT INTO subscriptions VALUES (?, ?)', (chat_id, activation))
        return sorted(active.items())

    def subscribe(self, chat_id, activation_ms):
        changed = self.main_store.subscribe(chat_id, activation_ms)
        self.subscribers()
        return changed

    def unsubscribe(self, chat_id):
        changed = self.main_store.unsubscribe(chat_id)
        self.subscribers()
        return changed

    def is_pending(self, chat_id, symbol, close_time):
        if self.eligible_symbol is not None and self.eligible_symbol(symbol) is not True:
            return False
        active = {chat_id: max(activation, self.activation_started_at_ms)
                  for chat_id, activation in self.main_store.subscribers()}
        if chat_id not in active:
            return False
        with self._lock:
            row = self._db.execute('SELECT activation_ms FROM subscriptions WHERE chat_id = ?',
                                   (chat_id,)).fetchone()
            if row is None or row[0] != active[chat_id]:
                return False
            return super().is_pending(chat_id, symbol, close_time)

    def pending(self, symbol=None):
        return [row for row in super().pending(symbol) if self.is_pending(*row[:3])]

    def get_delivery_blocked_until(self):
        return self.main_store.get_delivery_blocked_until()

    def set_delivery_blocked_until(self, timestamp):
        self.main_store.set_delivery_blocked_until(timestamp)

    def get_next_delivery_at(self):
        return self.main_store.get_next_delivery_at()

    def set_next_delivery_at(self, timestamp):
        self.main_store.set_next_delivery_at(timestamp)


class CombinedHistory:
    """Schedule each platform independently; never make Binance wait for Gate."""

    def __init__(self, primary, secondary=None):
        self.primary, self.secondary = primary, secondary
        self.services = [primary] + ([secondary] if secondary is not None else [])

    def request(self, chat_id):
        accepted = False
        for service in self.services:
            accepted = service.request(chat_id) or accepted
        return accepted

    def cached_result(self):
        for service in self.services:
            result = service.cached_result()
            if result is not None:
                return result
        return None

    def start_background(self):
        started = False
        for service in self.services:
            started = service.start_background() or started
        return started

    def is_alive(self):
        return any(service.is_alive() for service in self.services)

    def join(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        for service in self.services:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            service.join(remaining)

    def _errors(self, attribute):
        errors = []
        for index, service in enumerate(self.services):
            error = getattr(service, attribute, None)
            if isinstance(error, str) and error:
                label = getattr(service, 'exchange_name', None)
                if not isinstance(label, str):
                    label = 'Binance Spot' if index == 0 else 'Gate.io Spot'
                errors.append(label + ': ' + error)
        return '\n'.join(errors) or None

    @property
    def last_error(self):
        return self._errors('last_error')

    @property
    def refresh_error(self):
        return self._errors('refresh_error')


class ReportStore:
    """Merge confirmed ledger records without collapsing exchange identities."""

    def __init__(self, main_store, gate_store=None):
        self.main_store, self.gate_store = main_store, gate_store

    def is_admin(self, chat_id):
        return self.main_store.is_admin(chat_id)

    def report_rows(self, chat_id):
        rows = []
        for key, name, store in (('binance', 'Binance Spot', self.main_store),
                                 ('gate', 'Gate.io Spot', self.gate_store)):
            if store is not None:
                rows.extend(dict(row, exchange_name=name, quote_key=(key, row['symbol']))
                            for row in store.report_rows(chat_id))
        return sorted(rows, key=lambda row: (row['signal_close_time'], row['exchange_name'], row['symbol']))

    def get_ledger_started_at_ms(self):
        timestamps = [store.get_ledger_started_at_ms() for store in (self.main_store, self.gate_store)
                      if store is not None]
        timestamps = [value for value in timestamps if type(value) is int]
        return min(timestamps) if timestamps else None


class MarketRouter:
    """A quote must come from its original platform, with no cross-market fallback."""

    def __init__(self, binance_client, gate_client=None):
        self.binance_client, self.gate_client = binance_client, gate_client

    def latest_price(self, key):
        if isinstance(key, str):
            return self.binance_client.latest_price(key)
        if not isinstance(key, tuple) or len(key) != 2 or not isinstance(key[1], str):
            raise MarketError('Invalid report market identity.')
        source, symbol = key
        if source == 'binance':
            return self.binance_client.latest_price(symbol)
        if source == 'gate' and self.gate_client is not None:
            return self.gate_client.latest_price(symbol)
        raise MarketError('The original report market is unavailable.')
