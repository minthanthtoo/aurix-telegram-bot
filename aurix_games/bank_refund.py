"""Settled-order manual bank reimbursement; never initiates an external transfer."""
from datetime import datetime, timezone
import hashlib
import json
import re

from .payment_review import PaymentReviewError
from .order_state import OrderState


class SettledBankRefundService:
    def __init__(self, review):
        self.review = review
        self.payments = review.payments
        self.orders = review.repository
        self.ledger = self.payments.ledger

    def authorize(self, order_id, *, token, payload, now):
        actor = self.review.authenticate(token)
        required = {'rail','sender_fingerprint','merchant_reference','reason','original_sender_confirmed'}
        if not isinstance(payload,dict) or set(payload)!=required or payload['original_sender_confirmed'] is not True:
            raise PaymentReviewError('confirm the verified original sender and merchant settlement evidence')
        self._time(now)
        self._text(payload['merchant_reference']);self._text(payload['reason'])
        if payload['rail'] not in {'kpay','wavepay','aya','uab','cb'}:
            raise PaymentReviewError('unsupported refund rail')
        if not isinstance(payload['sender_fingerprint'],str) or not re.fullmatch(r'[a-f0-9]{64}',payload['sender_fingerprint']):
            raise PaymentReviewError('verified sender fingerprint required')
        order = self.orders.get(order_id)
        if order.state not in {OrderState.REVIEW_REFUND,OrderState.FAILED}:
            raise PaymentReviewError('settled-order bank payout requires explicit review_refund state')
        intent = self.orders.get_payment_intent(order_id)
        claim = self.payments.transfer_identity_claim_for_order(order_id)
        if intent is None or claim is None or claim['resolution']!='settlement' or intent['rail']!=payload['rail']:
            raise PaymentReviewError('original verified transfer and frozen receiver required')
        authorization = {**payload,'customer_id':order.customer_id,'amount_minor':order.snapshot.amount_minor,
            'currency':order.snapshot.currency,'incoming_transfer_key':claim['incoming_transfer_key'],
            'receiver_id':intent['receiver_id']}
        created = self.ledger.authorize_bank_refund(order_id,authorization=authorization,actor=actor,now=now)
        if order.state==OrderState.FAILED:
            self.orders.transition(order_id,OrderState.REVIEW_REFUND,at=now,
                reason='named reviewer authorized original-rail bank reimbursement')
        return {'order_id':order_id,'state':'bank_refund_pending','replayed':not created,
            'amount_minor':order.snapshot.amount_minor,'currency':order.snapshot.currency,'bank_transfer_initiated':False}

    def complete(self, order_id, *, token, payload, now):
        actor = self.review.authenticate(token)
        required = {'transaction_id','paid_at','amount_minor','currency','sender_fingerprint','merchant_reference','verified_paid'}
        if not isinstance(payload,dict) or set(payload)!=required or payload['verified_paid'] is not True:
            raise PaymentReviewError('independently verified outbound payment evidence required')
        self._time(now);self._text(payload['transaction_id']);self._text(payload['merchant_reference'])
        saved = self.ledger.bank_refund(order_id)
        if saved is None:
            raise PaymentReviewError('bank refund authorization required')
        authorization=json.loads(saved['authorization_json'])
        if (type(payload['amount_minor']) is not int or any(payload[k]!=authorization[k]
                for k in ('amount_minor','currency','sender_fingerprint'))):
            raise PaymentReviewError('outbound payment differs from authorized obligation')
        try:
            paid_at=datetime.fromisoformat(payload['paid_at'])
            if paid_at.utcoffset() is None or not datetime.fromisoformat(saved['authorized_at'])<=paid_at<=now:
                raise ValueError
        except (ValueError,TypeError):
            raise PaymentReviewError('outbound payment time is invalid') from None
        order=self.orders.get(order_id)
        if order.state not in {OrderState.REVIEW_REFUND,OrderState.REFUNDED}:
            raise PaymentReviewError('order changed before refund completion')
        incoming_key=self.payments.transfer_identity_key(rail=authorization['rail'],receiver_id=authorization['receiver_id'],
            currency=authorization['currency'],transaction_id=payload['transaction_id'])
        if incoming_key==authorization['incoming_transfer_key'] or self.ledger.contains_event_key(incoming_key):
            raise PaymentReviewError('an incoming transaction cannot prove an outbound refund')
        key='refund:'+hashlib.sha256(json.dumps([authorization['rail'],authorization['currency'],payload['transaction_id']],
            separators=(',',':')).encode()).hexdigest()
        self.review._initialize()
        from persistence import open_sqlite_connection
        with open_sqlite_connection(self.orders.path) as db:
            if db.execute('SELECT 1 FROM game_payment_external_refund_completions WHERE refund_transfer_key=?',(key,)).fetchone():
                raise PaymentReviewError('outbound transfer already belongs to an exception refund')
        completion={**payload,'paid_at':paid_at.astimezone(timezone.utc).isoformat(),'actor':actor}
        created=self.ledger.complete_bank_refund(order_id,completion=completion,outbound_key=key,now=now)
        if order.state!=OrderState.REFUNDED:
            self.orders.transition(order_id,OrderState.REFUNDED,at=now,reason='verified original-rail bank reimbursement completed; no wallet credit')
        return {'order_id':order_id,'state':'refunded','route':'bank','replayed':not created,'wallet_credit_created':False}

    def status(self, order_id, *, token):
        self.review.authenticate(token)
        row=self.ledger.bank_refund(order_id)
        if row is None:return {'order_id':order_id,'state':'not_authorized'}
        authorization=json.loads(row['authorization_json'])
        return {'order_id':order_id,'state':'bank_refund_completed' if row['completed_at'] else 'bank_refund_pending',
            'amount_minor':authorization['amount_minor'],'currency':authorization['currency'],
            'authorized_at':row['authorized_at'],'completed_at':row['completed_at'],'route':'bank'}

    @staticmethod
    def _text(value):
        if not isinstance(value,str) or not 1<=len(value)<=240 or value!=value.strip() or any(ord(c)<32 for c in value):
            raise PaymentReviewError('invalid payment evidence text')

    @staticmethod
    def _time(value):
        if not isinstance(value,datetime) or value.utcoffset() is None:
            raise PaymentReviewError('timezone-aware timestamp required')
