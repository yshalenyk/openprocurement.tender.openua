# -*- coding: utf-8 -*-
from pkg_resources import get_distribution
from datetime import timedelta
import re
from logging import getLogger
from openprocurement.api.models import get_now, TZ
from openprocurement.api.utils import (
    check_tender_status,
    context_unpack,
    calculate_business_date,
)
from barbecue import chef
PKG = get_distribution(__package__)
LOGGER = getLogger(PKG.project_name)
BLOCK_COMPLAINT_STATUS = ['claim', 'pending', 'accepted', 'satisfied', 'stopping']
PENDING_COMPLAINT_STATUS = ['claim', 'answered', 'pending', 'accepted', 'satisfied', 'stopping']


def check_bids(request):
    tender = request.validated['tender']
    if tender.lots:
        [setattr(i.auctionPeriod, 'startDate', None) for i in tender.lots if i.numberOfBids < 2 and i.auctionPeriod and i.auctionPeriod.startDate]
        [setattr(i, 'status', 'unsuccessful') for i in tender.lots if i.numberOfBids < 2 and i.status == 'active']
        [setattr(i, 'date', get_now()) for i in tender.lots if i.status == 'unsuccessful']
        if not set([i.status for i in tender.lots]).difference(set(['unsuccessful', 'cancelled'])):
            tender.status = 'unsuccessful'
            tender.date = get_now()
    else:
        if tender.numberOfBids < 2:
            if tender.auctionPeriod and tender.auctionPeriod.startDate:
                tender.auctionPeriod.startDate = None
            tender.status = 'unsuccessful'
            tender.date = get_now()


def check_complaint_status(request, complaint):
    if complaint.status == 'answered':
        complaint.status = complaint.resolutionType


def check_status(request):
    tender = request.validated['tender']
    now = get_now()
    if not tender.lots and tender.status == 'active.tendering' and tender.tenderPeriod.endDate <= now and \
        not any([i.status in BLOCK_COMPLAINT_STATUS for i in tender.complaints]) and \
        not any([i.id for i in tender.questions if not i.answer]):
        for complaint in tender.complaints:
            check_complaint_status(request, complaint)
        LOGGER.info('Switched tender {} to {}'.format(tender['id'], 'active.auction'),
                    extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_active.auction'}))
        tender.status = 'active.auction'
        check_bids(request)
        if tender.numberOfBids < 2 and tender.auctionPeriod:
            tender.auctionPeriod.startDate = None
        return
    elif tender.lots and tender.status == 'active.tendering' and tender.tenderPeriod.endDate <= now and \
        not any([i.status in BLOCK_COMPLAINT_STATUS for i in tender.complaints]) and \
        not any([i.id for i in tender.questions if not i.answer]):
        for complaint in tender.complaints:
            check_complaint_status(request, complaint)
        LOGGER.info('Switched tender {} to {}'.format(tender['id'], 'active.auction'),
                    extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_active.auction'}))
        tender.status = 'active.auction'
        check_bids(request)
        [setattr(i.auctionPeriod, 'startDate', None) for i in tender.lots if i.numberOfBids < 2 and i.auctionPeriod]
        return
    elif not tender.lots and tender.status == 'active.awarded':
        standStillEnds = [
            a.complaintPeriod.endDate.astimezone(TZ)
            for a in tender.awards
            if a.complaintPeriod.endDate
        ]
        if not standStillEnds:
            return
        standStillEnd = max(standStillEnds)
        if standStillEnd <= now:
            pending_complaints = any([
                i['status'] in PENDING_COMPLAINT_STATUS
                for i in tender.complaints
            ])
            pending_awards_complaints = any([
                i['status'] in PENDING_COMPLAINT_STATUS
                for a in tender.awards
                for i in a.complaints
            ])
            awarded = any([
                i['status'] == 'active'
                for i in tender.awards
            ])
            if not pending_complaints and not pending_awards_complaints and not awarded:
                LOGGER.info('Switched tender {} to {}'.format(tender.id, 'unsuccessful'),
                            extra=context_unpack(request, {'MESSAGE_ID': 'switched_tender_unsuccessful'}))
                check_tender_status(request)
                return
    elif tender.lots and tender.status in ['active.qualification', 'active.awarded']:
        if any([i['status'] in PENDING_COMPLAINT_STATUS and i.relatedLot is None for i in tender.complaints]):
            return
        for lot in tender.lots:
            if lot['status'] != 'active':
                continue
            lot_awards = [i for i in tender.awards if i.lotID == lot.id]
            standStillEnds = [
                a.complaintPeriod.endDate.astimezone(TZ)
                for a in lot_awards
                if a.complaintPeriod.endDate
            ]
            if not standStillEnds:
                continue
            standStillEnd = max(standStillEnds)
            if standStillEnd <= now:
                pending_complaints = any([
                    i['status'] in PENDING_COMPLAINT_STATUS and i.relatedLot == lot.id
                    for i in tender.complaints
                ])
                pending_awards_complaints = any([
                    i['status'] in PENDING_COMPLAINT_STATUS
                    for a in lot_awards
                    for i in a.complaints
                ])
                awarded = any([
                    i['status'] == 'active'
                    for i in lot_awards
                ])
                if not pending_complaints and not pending_awards_complaints and not awarded:
                    LOGGER.info('Switched lot {} of tender {} to {}'.format(lot['id'], tender.id, 'unsuccessful'),
                                extra=context_unpack(request, {'MESSAGE_ID': 'switched_lot_unsuccessful'}, {'LOT_ID': lot['id']}))
                    check_tender_status(request)


def add_next_award(request):
    tender = request.validated['tender']
    now = get_now()
    if not tender.awardPeriod:
        tender.awardPeriod = type(tender).awardPeriod({})
    if not tender.awardPeriod.startDate:
        tender.awardPeriod.startDate = now
    if tender.lots:
        statuses = set()
        for lot in tender.lots:
            if lot.status != 'active':
                continue
            lot_awards = [i for i in tender.awards if i.lotID == lot.id]
            if lot_awards and lot_awards[-1].status in ['pending', 'active']:
                statuses.add(lot_awards[-1].status if lot_awards else 'unsuccessful')
                continue
            lot_items = [i.id for i in tender.items if i.relatedLot == lot.id]
            features = [
                i
                for i in (tender.features or [])
                if i.featureOf == 'tenderer' or i.featureOf == 'lot' and i.relatedItem == lot.id or i.featureOf == 'item' and i.relatedItem in lot_items
            ]
            codes = [i.code for i in features]
            bids = [
                {
                    'id': bid.id,
                    'value': [i for i in bid.lotValues if lot.id == i.relatedLot][0].value,
                    'tenderers': bid.tenderers,
                    'parameters': [i for i in bid.parameters if i.code in codes],
                    'date': [i for i in bid.lotValues if lot.id == i.relatedLot][0].date
                }
                for bid in tender.bids
                if lot.id in [i.relatedLot for i in bid.lotValues] and bid.status == "active"
            ]
            if not bids:
                lot.status = 'unsuccessful'
                lot.date = get_now()
                statuses.add('unsuccessful')
                continue
            unsuccessful_awards = [i.bid_id for i in lot_awards if i.status == 'unsuccessful']
            bids = chef(bids, features, unsuccessful_awards)
            if bids:
                bid = bids[0]
                award = tender.__class__.awards.model_class({
                    'bid_id': bid['id'],
                    'lotID': lot.id,
                    'status': 'pending',
                    'value': bid['value'],
                    'suppliers': bid['tenderers'],
                    'complaintPeriod': {
                        'startDate': now.isoformat()
                    }
                })
                tender.awards.append(award)
                request.response.headers['Location'] = request.route_url('Tender Awards', tender_id=tender.id, award_id=award['id'])
                statuses.add('pending')
            else:
                statuses.add('unsuccessful')
        if statuses.difference(set(['unsuccessful', 'active'])):
            tender.awardPeriod.endDate = None
            tender.status = 'active.qualification'
        else:
            tender.awardPeriod.endDate = now
            tender.status = 'active.awarded'
    else:
        if not tender.awards or tender.awards[-1].status not in ['pending', 'active']:
            unsuccessful_awards = [i.bid_id for i in tender.awards if i.status == 'unsuccessful']
            active_bids = [bid for bid in tender.bids if bid.status == "active"]
            bids = chef(active_bids, tender.features or [], unsuccessful_awards)
            if bids:
                bid = bids[0].serialize()
                award = tender.__class__.awards.model_class({
                    'bid_id': bid['id'],
                    'status': 'pending',
                    'value': bid['value'],
                    'suppliers': bid['tenderers'],
                    'complaintPeriod': {
                        'startDate': get_now().isoformat()
                    }
                })
                tender.awards.append(award)
                request.response.headers['Location'] = request.route_url('Tender Awards', tender_id=tender.id, award_id=award['id'])
        if tender.awards[-1].status == 'pending':
            tender.awardPeriod.endDate = None
            tender.status = 'active.qualification'
        else:
            tender.awardPeriod.endDate = now
            tender.status = 'active.awarded'

