"""Checks for the Phase 6 core rules (C1-C9) and AI features 2 and 5."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, User
from ai.gateway import AIGateway, ClassicAdapter
from ai.matching import MatchingService
from ai.models import AIRun
from catalog.models import Asset, AssetEvent, Category
from catalog.services import CatalogService
from core.errors import Conflict, DomainError, IllegalTransition
from notifications.models import Notification
from reports.services import ImpactService, ReportService

from .models import ResourceRequest, Transfer
from .services import ExchangeService


class BrokenAdapter:
    name, model_id = 'broken', 'broken-model'

    def relevance(self, need, candidates):
        raise TimeoutError('provider did not answer')

    def summarise(self, snapshot):
        raise TimeoutError('provider did not answer')


@override_settings(AI_PROVIDER='classic')
class CampusTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.eng = Department.objects.create(code='ENG', name='Engineering')
        cls.cs = Department.objects.create(code='CS', name='Computer Science')

        def user(name, role, department=None):
            return User.objects.create_user(name, password='pw', role=role, department=department)

        cls.custodian = user('custodian', User.Role.CUSTODIAN, cls.eng)
        cls.cs_custodian = user('cs_custodian', User.Role.CUSTODIAN, cls.cs)
        cls.requester = user('requester', User.Role.REQUESTER, cls.cs)
        cls.other_requester = user('other', User.Role.REQUESTER, cls.cs)
        cls.approver = user('approver', User.Role.APPROVER, cls.cs)
        cls.approver2 = user('approver2', User.Role.APPROVER, cls.eng)
        cls.admin = user('boss', User.Role.ADMIN)
        cls.desktops = Category.objects.create(
            name='Desktop computers', warm_source_reduction=Decimal('-20.86'),
            warm_landfill=Decimal('0.02'))
        cls.chairs = Category.objects.create(name='Chairs')

    def make_asset(self, name='Lab stool', category=None, quantity=1, publish=True, **extra):
        asset = CatalogService.register(
            self.custodian, name=name, description=extra.pop('description', ''),
            category=category or self.chairs, condition=extra.pop('condition', 'GOOD'),
            quantity=quantity, **extra)
        if publish:
            CatalogService.publish(asset, self.custodian)
        return asset

    def make_request(self, requester=None, quantity=1, **extra):
        data = {'title': 'Chairs for a lab', 'quantity': quantity, 'min_condition': 'FAIR'}
        data.update(extra)
        return ExchangeService.submit(requester or self.requester, **data)

    def run_to_receipt(self, asset, resource_request):
        reservation = ExchangeService.reserve(
            resource_request, asset.pk, resource_request.quantity, resource_request.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        return ExchangeService.receive(reservation.transfer, resource_request.requester)


class PermissionTests(CampusTestCase):
    """C1: the server checks permissions."""

    def test_requester_cannot_register_asset_even_by_posting(self):
        self.client.force_login(self.requester)
        response = self.client.post(reverse('asset_create'), {
            'name': 'Sneaky', 'category': self.chairs.pk, 'condition': 'GOOD', 'quantity': 1})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Asset.objects.exists())

    def test_requester_cannot_open_approval_queue_or_report(self):
        self.client.force_login(self.requester)
        self.assertEqual(self.client.get(reverse('approval_queue')).status_code, 403)
        self.assertEqual(self.client.get(reverse('report')).status_code, 403)

    def test_custodian_cannot_publish_another_departments_asset(self):
        asset = self.make_asset(publish=False)
        with self.assertRaises(PermissionDenied):
            CatalogService.publish(asset, self.cs_custodian)

    def test_admin_has_no_approval_right(self):
        reservation = ExchangeService.reserve(self.make_request(), self.make_asset().pk, 1, self.requester)
        with self.assertRaises(DomainError):
            ExchangeService.decide(reservation.approval_case, self.admin, True)

    def test_anonymous_user_is_sent_to_login(self):
        response = self.client.get(reverse('marketplace'))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('marketplace')}")

    def test_user_cannot_see_someone_elses_request(self):
        resource_request = self.make_request()
        self.client.force_login(self.other_requester)
        self.assertEqual(
            self.client.get(reverse('request_detail', args=[resource_request.pk])).status_code, 403)


class CatalogTests(CampusTestCase):
    """C2, C3, C6."""

    def test_register_uses_custodians_department_and_starts_recorded(self):
        asset = self.make_asset(publish=False)
        self.assertEqual(asset.department, self.eng)
        self.assertEqual(asset.state, Asset.State.RECORDED)

    def test_search_filters_by_keyword_category_condition_department(self):
        stool = self.make_asset('Lab stool', condition='FAIR')
        self.make_asset('Dell desktop', category=self.desktops)
        self.make_asset('Hidden stool', publish=False)
        self.assertEqual(list(CatalogService.search(keyword='stool')), [stool])
        self.assertEqual(list(CatalogService.search(category=self.chairs)), [stool])
        self.assertEqual(list(CatalogService.search(condition='FAIR')), [stool])
        self.assertEqual(CatalogService.search(department=self.cs).count(), 0)

    def test_illegal_state_move_is_refused(self):
        asset = self.make_asset(publish=False)
        with self.assertRaises(IllegalTransition):
            asset.transition(Asset.State.IN_USE, self.custodian, 'Skip the cycle')
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.RECORDED)

    def test_history_records_who_did_what_and_cannot_be_changed(self):
        asset = self.make_asset()
        events = list(asset.events.all())
        self.assertEqual([e.action for e in events], ['Registered', 'Published'])
        self.assertEqual(events[1].actor, self.custodian)
        events[0].note = 'edited'
        with self.assertRaises(DomainError):
            events[0].save()
        with self.assertRaises(DomainError):
            events[0].delete()


class ExchangeTests(CampusTestCase):
    """C4, C5, C6, C8."""

    def test_full_cycle_changes_owner_and_department_on_receipt(self):
        asset = self.make_asset()
        resource_request = self.make_request()
        self.run_to_receipt(asset, resource_request)
        asset.refresh_from_db()
        resource_request.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.IN_USE)
        self.assertEqual(asset.department, self.cs)
        self.assertEqual(asset.owner, self.requester)
        self.assertEqual(resource_request.state, ResourceRequest.State.FULFILLED)
        self.assertEqual(
            [e.to_state for e in asset.events.all()],
            ['RECORDED', 'AVAILABLE', 'HELD', 'IN_TRANSIT', 'IN_USE'])

    def test_owner_does_not_change_before_receipt(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        asset.refresh_from_db()
        self.assertEqual(asset.department, self.eng)

    def test_last_item_cannot_go_to_two_people(self):
        asset = self.make_asset(quantity=1)
        ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        with self.assertRaises(DomainError):
            ExchangeService.reserve(
                self.make_request(self.other_requester), asset.pk, 1, self.other_requester)
        self.assertEqual(asset.reservations.count(), 1)

    def test_stale_copy_of_asset_cannot_take_it_again(self):
        asset = self.make_asset()
        stale = Asset.objects.get(pk=asset.pk)
        asset.transition(Asset.State.HELD, self.custodian, 'Reserved')
        with self.assertRaises(Conflict):
            stale.transition(Asset.State.HELD, self.custodian, 'Reserved')

    def test_partial_reservation_splits_the_group(self):
        asset = self.make_asset(quantity=5)
        reservation = ExchangeService.reserve(self.make_request(quantity=2), asset.pk, 2, self.requester)
        asset.refresh_from_db()
        self.assertEqual((asset.quantity, asset.state), (3, Asset.State.AVAILABLE))
        self.assertEqual((reservation.asset.quantity, reservation.asset.state), (2, Asset.State.HELD))
        self.assertEqual(reservation.asset.parent, asset)

    def test_no_self_approval(self):
        asset = self.make_asset()
        resource_request = self.make_request(self.approver)
        reservation = ExchangeService.reserve(resource_request, asset.pk, 1, self.approver)
        with self.assertRaises(DomainError):
            ExchangeService.decide(reservation.approval_case, self.approver, True)
        self.client.force_login(self.approver)
        response = self.client.post(
            reverse('approval_decide', args=[reservation.approval_case.pk]), {'decision': 'approve'})
        self.assertEqual(response.status_code, 403)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)

    def test_rejection_needs_reason_and_releases_the_hold(self):
        asset = self.make_asset()
        resource_request = self.make_request()
        reservation = ExchangeService.reserve(resource_request, asset.pk, 1, self.requester)
        with self.assertRaises(DomainError):
            ExchangeService.decide(reservation.approval_case, self.approver2, False, ' ')
        ExchangeService.decide(reservation.approval_case, self.approver2, False, 'Still needed')
        asset.refresh_from_db()
        resource_request.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)
        self.assertEqual(resource_request.state, ResourceRequest.State.REJECTED)

    def test_cannot_dispatch_before_approval_or_receive_before_dispatch(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        self.assertFalse(Transfer.objects.exists())
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        with self.assertRaises(DomainError):
            ExchangeService.receive(reservation.transfer, self.requester)

    def test_only_the_receiver_confirms_and_a_second_click_changes_nothing(self):
        asset = self.make_asset()
        resource_request = self.make_request()
        reservation = ExchangeService.reserve(resource_request, asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        with self.assertRaises(PermissionDenied):
            ExchangeService.receive(reservation.transfer, self.custodian)
        ExchangeService.receive(reservation.transfer, self.requester)
        events = AssetEvent.objects.count()
        ExchangeService.receive(reservation.transfer, self.requester)
        self.assertEqual(AssetEvent.objects.count(), events)

    def test_partial_fulfilment_keeps_request_open(self):
        asset = self.make_asset(quantity=2)
        resource_request = self.make_request(quantity=4)
        reservation = ExchangeService.reserve(resource_request, asset.pk, 2, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        ExchangeService.receive(reservation.transfer, self.requester)
        resource_request.refresh_from_db()
        self.assertEqual(resource_request.state, ResourceRequest.State.PART_FULFILLED)
        self.assertEqual(resource_request.remaining_quantity(), 2)
        self.assertTrue(resource_request.is_open_for_matching)

    def test_cancel_releases_hold_but_not_while_in_transit(self):
        asset = self.make_asset()
        resource_request = self.make_request()
        reservation = ExchangeService.reserve(resource_request, asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        with self.assertRaises(DomainError):
            ExchangeService.cancel(resource_request, self.requester)

        second_asset = self.make_asset('Second stool')
        second = self.make_request(self.other_requester)
        ExchangeService.reserve(second, second_asset.pk, 1, self.other_requester)
        ExchangeService.cancel(second, self.other_requester)
        second_asset.refresh_from_db()
        self.assertEqual(second_asset.state, Asset.State.AVAILABLE)

    def test_received_asset_can_be_declared_surplus_by_new_department(self):
        asset = self.make_asset()
        self.run_to_receipt(asset, self.make_request())
        asset.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            CatalogService.publish(asset, self.custodian)  # no longer Engineering's
        CatalogService.publish(asset, self.cs_custodian)
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)

    def test_notifications_for_made_approved_rejected_received(self):
        asset = self.make_asset()
        resource_request = self.make_request()
        self.assertTrue(self.requester.notifications.filter(message__contains='submitted').exists())
        reservation = ExchangeService.reserve(resource_request, asset.pk, 1, self.requester)
        self.assertTrue(self.approver2.notifications.filter(message__contains='needs approval').exists())
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        self.assertTrue(self.requester.notifications.filter(message__contains='approved').exists())
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        ExchangeService.receive(reservation.transfer, self.requester)
        self.assertTrue(self.custodian.notifications.filter(message__contains='received').exists())

        other_asset = self.make_asset('Other stool')
        other = self.make_request(self.other_requester)
        other_reservation = ExchangeService.reserve(other, other_asset.pk, 1, self.other_requester)
        ExchangeService.decide(other_reservation.approval_case, self.approver2, False, 'No')
        self.assertTrue(self.other_requester.notifications.filter(message__contains='rejected').exists())

    def test_failed_action_leaves_no_notification_behind(self):
        asset = self.make_asset()
        ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        before = Notification.objects.count()
        second = self.make_request(self.other_requester)
        after_submit = Notification.objects.count()
        with self.assertRaises(DomainError):
            ExchangeService.reserve(second, asset.pk, 1, self.other_requester)
        self.assertEqual(Notification.objects.count(), after_submit)
        self.assertEqual(after_submit, before + 1)


class MatchingTests(CampusTestCase):
    """AI feature 2."""

    def test_top_five_with_score_and_reason_and_hard_rules(self):
        for index in range(7):
            self.make_asset(f'Lab stool {index}', description='Stool for lab benches')
        self.make_asset('Poor stool', condition='POOR')
        self.make_asset('Dell desktop', category=self.desktops)
        self.make_asset('Unpublished stool', publish=False)
        resource_request = self.make_request(
            title='Stools for a lab', category=self.chairs, min_condition='FAIR')
        matches = MatchingService().find_matches(resource_request, self.requester)
        self.assertEqual(len(matches), 5)
        self.assertEqual([m.rank for m in matches], [1, 2, 3, 4, 5])
        scores = [m.score for m in matches]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for match in matches:
            self.assertTrue(match.reason)
            self.assertTrue(0 <= match.score <= 100)
            self.assertEqual(match.asset.category, self.chairs)
            self.assertNotEqual(match.asset.condition, 'POOR')

    def test_better_fit_ranks_first(self):
        self.make_asset('Office desk', description='Desk with drawers')
        stool = self.make_asset('Lab stool', description='Stool for lab benches')
        resource_request = self.make_request(title='Stool for lab benches')
        matches = MatchingService().find_matches(resource_request, self.requester)
        self.assertEqual(matches[0].asset, stool)

    def test_own_department_items_are_never_offered(self):
        own = CatalogService.register(
            self.cs_custodian, name='Lab stool', description='', category=self.chairs,
            condition='GOOD', quantity=1)
        CatalogService.publish(own, self.cs_custodian)
        self.assertEqual(MatchingService().find_matches(self.make_request(), self.requester), [])

    def test_provider_failure_falls_back_and_is_recorded(self):
        self.make_asset('Lab stool')
        service = MatchingService(AIGateway(adapter=BrokenAdapter()))
        matches = service.find_matches(self.make_request(), self.requester)
        self.assertEqual(len(matches), 1)
        run = matches[0].ai_run
        self.assertEqual(run.adapter, 'classic')
        self.assertIn('TimeoutError', run.fallback_reason)

    def test_only_minimum_fields_are_sent_to_the_ai(self):
        self.make_asset('Lab stool', replacement_unit_cost=Decimal('180'), location='Room 4')
        MatchingService().find_matches(self.make_request(), self.requester)
        payload = AIRun.objects.get().input_snapshot
        self.assertEqual(set(payload['candidates'][0]),
                         {'id', 'name', 'description', 'category', 'condition'})
        self.assertNotIn('requester', str(payload))

    def test_accept_reserves_and_reject_hides_the_item(self):
        first = self.make_asset('Lab stool A')
        self.make_asset('Lab stool B')
        resource_request = self.make_request()
        matches = MatchingService().find_matches(resource_request, self.requester)
        self.client.force_login(self.requester)
        rejected = matches[0]
        self.client.post(reverse('recommendation_decide', args=[rejected.pk]), {'decision': 'reject'})
        again = MatchingService().find_matches(resource_request, self.requester)
        self.assertNotIn(rejected.asset, [m.asset for m in again])
        self.client.post(reverse('recommendation_decide', args=[again[0].pk]), {'decision': 'accept'})
        again[0].asset.refresh_from_db()
        self.assertEqual(again[0].asset.state, Asset.State.HELD)
        self.assertIsNotNone(first)

    def test_other_user_cannot_accept_my_recommendation(self):
        self.make_asset('Lab stool')
        match = MatchingService().find_matches(self.make_request(), self.requester)[0]
        self.client.force_login(self.other_requester)
        response = self.client.post(reverse('recommendation_decide', args=[match.pk]), {'decision': 'accept'})
        self.assertEqual(response.status_code, 403)


class ReportTests(CampusTestCase):
    """C7, C9 and AI feature 5."""

    def setUp(self):
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=7)

    def reuse(self, **asset_fields):
        asset = self.make_asset(**asset_fields)
        self.run_to_receipt(asset, self.make_request(quantity=asset.quantity))
        return asset

    def test_measures_follow_the_documented_formulas(self):
        # SRS 6.1 example: 3 chairs at AED 200 avoid AED 600.
        self.reuse(quantity=3, replacement_unit_cost=Decimal('200'), unit_mass_kg=Decimal('5'))
        # SRS 6.2 factors on 100 kg of desktops, landfill baseline vs source reduction.
        self.reuse(name='Desktop', category=self.desktops, quantity=10, unit_mass_kg=Decimal('10'))
        measures = ImpactService.calculate(self.start, self.today)['measures']
        self.assertEqual(measures['items_reused']['value'], 13)
        self.assertEqual(measures['cost_avoided_aed']['value'], 600.0)
        self.assertEqual(measures['cost_avoided_aed']['missing'], 10)
        self.assertEqual(measures['waste_diverted_kg']['value'], 115.0)
        expected = round(100 / 907.185 * (0.02 + 20.86) * 1000, 2)
        self.assertAlmostEqual(measures['co2e_kg']['value'], expected, places=1)
        self.assertEqual(measures['co2e_kg']['missing'], 3)

    def test_missing_data_is_not_available_rather_than_zero(self):
        self.reuse()
        measures = ImpactService.calculate(self.start, self.today)['measures']
        self.assertIsNone(measures['cost_avoided_aed']['value'])
        self.assertIsNone(measures['co2e_kg']['value'])

    def test_unreceived_transfers_do_not_count(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        self.assertEqual(ImpactService.calculate(self.start, self.today)['measures']['items_reused']['value'], 0)

    def test_dashboard_hides_cost_from_requesters(self):
        self.reuse(replacement_unit_cost=Decimal('200'))
        self.client.force_login(self.requester)
        self.assertNotContains(self.client.get(reverse('dashboard')), 'Cost avoided')
        self.client.force_login(self.approver)
        self.assertContains(self.client.get(reverse('dashboard')), 'Cost avoided')

    def test_csv_export(self):
        self.reuse()
        self.client.force_login(self.approver)
        response = self.client.get(reverse('report'), {'format': 'csv'})
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        lines = response.content.decode('utf-8-sig').strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('REQ-', lines[1])
        self.assertIn('Completed', lines[1])

    def test_summary_numbers_come_from_the_database_and_admin_publishes(self):
        self.reuse(quantity=3, replacement_unit_cost=Decimal('200'))
        draft = ReportService().generate(self.start, self.today, self.approver)
        self.assertEqual(draft.snapshot['measures']['items_reused']['value'], 3)
        self.assertIn('3 item(s)', draft.text)
        self.assertEqual(ReportService.unverified_numbers(draft), [])
        with self.assertRaises(PermissionDenied):
            ReportService.publish(draft, self.approver)
        ReportService.save_edit(draft, self.admin, draft.text + ' Well done.')
        ReportService.publish(draft, self.admin)
        self.assertEqual(draft.status, 'PUBLISHED')
        self.assertNotEqual(draft.text, draft.ai_text)
        with self.assertRaises(DomainError):
            ReportService.save_edit(draft, self.admin, 'changed after publishing')

    def test_invented_number_in_narrative_is_flagged(self):
        self.reuse()
        draft = ReportService().generate(self.start, self.today, self.approver)
        draft.text = 'We reused 1 item and saved 99,000 AED.'
        self.assertEqual(ReportService.unverified_numbers(draft), ['99000'])

    def test_summary_falls_back_when_provider_fails(self):
        draft = ReportService(AIGateway(adapter=BrokenAdapter())).generate(
            self.start, self.today, self.admin)
        self.assertTrue(draft.ai_run.used_fallback)
        self.assertEqual(draft.ai_text, ClassicAdapter().summarise(draft.snapshot))

    def test_summary_sends_no_names_to_the_ai(self):
        self.reuse(name='Secret prototype stool')
        draft = ReportService().generate(self.start, self.today, self.admin)
        self.assertNotIn('Secret', str(draft.ai_run.input_snapshot))
        self.assertNotIn(self.requester.username, str(draft.ai_run.input_snapshot))


class ClassificationTests(CampusTestCase):
    """AI feature 1."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.monitors = Category.objects.create(name='Monitors')

    def test_suggests_category_condition_tags_and_confidence(self):
        from ai.classification import ClassificationService
        result = ClassificationService().suggest(
            self.custodian, 'Dell 24-inch monitor', 'small scratch on the bezel')
        self.assertEqual(result['category'], self.monitors)
        self.assertEqual(result['condition'], 'GOOD')
        self.assertIn('monitor', result['tags'])
        self.assertTrue(0 < result['confidence'] <= 1)

    def test_broken_item_is_suggested_as_poor(self):
        from ai.classification import ClassificationService
        result = ClassificationService().suggest(self.custodian, 'Office chair', 'broken armrest')
        self.assertEqual((result['category'], result['condition']), (self.chairs, 'POOR'))

    def test_suggest_button_saves_nothing_and_fills_the_form(self):
        self.client.force_login(self.custodian)
        response = self.client.post(reverse('asset_create'), {
            'action': 'suggest', 'name': 'Dell 24-inch monitor', 'description': 'small scratch'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Asset.objects.exists())
        self.assertContains(response, 'AI suggestion')
        self.assertEqual(response.context['form'].initial['category'], str(self.monitors.pk))

    def test_history_records_whether_suggestion_was_accepted_or_edited(self):
        self.client.force_login(self.custodian)
        response = self.client.post(reverse('asset_create'), {
            'action': 'suggest', 'name': 'Dell 24-inch monitor', 'description': 'small scratch'})
        run = response.context['suggestion']['run']
        data = {'name': 'Dell 24-inch monitor', 'description': 'small scratch', 'quantity': 1,
                'category': self.monitors.pk, 'tags': 'dell, monitor', 'ai_run': run.pk}
        self.client.post(reverse('asset_create'), {**data, 'condition': 'GOOD'})
        self.client.post(reverse('asset_create'), {**data, 'condition': 'FAIR'})
        newest, oldest = Asset.objects.all()
        self.assertIn('accepted', oldest.events.first().note)
        self.assertIn('edited', newest.events.first().note)
        self.assertEqual(oldest.tags, 'dell, monitor')

    def test_register_without_asking_the_ai_works(self):
        self.client.force_login(self.custodian)
        for run in ('', 'abc'):
            response = self.client.post(reverse('asset_create'), {
                'name': 'Office chair', 'description': '', 'category': self.chairs.pk,
                'condition': 'GOOD', 'quantity': 1, 'ai_run': run})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(Asset.objects.count(), 2)
        self.assertEqual(Asset.objects.first().events.first().note, '')

    def test_requester_cannot_ask_for_a_suggestion(self):
        self.client.force_login(self.requester)
        response = self.client.post(reverse('asset_create'), {'action': 'suggest', 'name': 'x'})
        self.assertEqual(response.status_code, 403)


class AdviceTests(CampusTestCase):
    """AI feature 3."""

    def advise(self, **fields):
        from ai.advice import AdviceService
        asset = self.make_asset(**fields)
        return asset, AdviceService().advise(asset, self.custodian)

    def test_good_item_reuse_broken_item_repair_unsafe_item_recycle(self):
        self.assertEqual(self.advise(name='Lab stool')[1].action, 'REUSE')
        self.assertEqual(self.advise(name='Projector', description='bulb broken')[1].action, 'REPAIR')
        self.assertEqual(self.advise(name='Heater', description='unsafe wiring')[1].action, 'RECYCLE')
        self.assertEqual(self.advise(name='Old stool', condition='FAIR')[1].action, 'DONATE')

    def test_impact_numbers_come_from_our_formulas(self):
        asset, advice = self.advise(
            name='Desktop', category=self.desktops, quantity=10,
            replacement_unit_cost=Decimal('200'), unit_mass_kg=Decimal('10'))
        self.assertEqual(advice.impact['replacement_cost_aed'], 2000.0)
        self.assertEqual(advice.impact['waste_diverted_kg'], 100.0)
        self.assertAlmostEqual(advice.impact['co2e_kg'], 2301.63, places=1)
        self.assertNotIn('200', str(advice.ai_run.input_snapshot))  # cost is never sent

    def test_missing_data_gives_not_available(self):
        _, advice = self.advise()
        self.assertEqual(advice.impact, {
            'replacement_cost_aed': None, 'waste_diverted_kg': None, 'co2e_kg': None})

    def test_custodian_decides_and_asset_state_does_not_change(self):
        from ai.advice import AdviceService
        asset, advice = self.advise()
        AdviceService.decide(advice, self.custodian, accepted=True)
        asset.refresh_from_db()
        advice.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)
        self.assertEqual(advice.decision, 'ACCEPTED')
        self.assertEqual(asset.events.last().action, 'Advice accepted')
        with self.assertRaises(DomainError):
            AdviceService.decide(advice, self.custodian, accepted=False)

    def test_only_the_owning_custodian_gets_advice(self):
        from ai.advice import AdviceService
        asset = self.make_asset()
        with self.assertRaises(PermissionDenied):
            AdviceService().advise(asset, self.cs_custodian)
        self.client.force_login(self.requester)
        self.assertEqual(
            self.client.post(reverse('asset_action', args=[asset.pk, 'advise'])).status_code, 403)

    def test_falls_back_when_provider_fails(self):
        from ai.advice import AdviceService
        advice = AdviceService(AIGateway(adapter=BrokenAdapter())).advise(self.make_asset(), self.custodian)
        self.assertTrue(advice.ai_run.used_fallback)


class AssistantTests(CampusTestCase):
    """AI feature 4."""

    def ask(self, question, user=None):
        from ai.assistant import AssistantService
        return AssistantService().ask(user or self.requester, question)

    def test_lists_only_real_matching_items(self):
        projector = self.make_asset('Epson projector')
        self.make_asset('Lab stool')
        self.make_asset('Hidden projector', publish=False)
        answer = self.ask('Any projectors in Engineering?')
        self.assertEqual(answer['item_ids'], [projector.pk])
        self.assertIn('1 available item', answer['answer'])

    def test_says_i_dont_know_when_data_has_no_answer(self):
        self.make_asset('Lab stool')
        for question in ('What is the weather tomorrow?', 'Any projectors in Business?'):
            answer = self.ask(question)
            self.assertIn("I don't know", answer['answer'])
            self.assertEqual(answer['item_ids'], [])

    def test_answers_about_my_own_requests_only(self):
        self.make_request(title='Chairs for a lab')
        self.make_request(self.other_requester, title='Secret other request')
        answer = self.ask('What is the status of my requests?')
        self.assertIn('Chairs for a lab: Submitted', answer['answer'])
        self.assertNotIn('Secret', answer['answer'])
        self.assertNotIn('Secret', str(AIRun.objects.latest('id').input_snapshot))

    def test_asking_never_changes_anything(self):
        asset = self.make_asset('Epson projector')
        before = (AssetEvent.objects.count(), ResourceRequest.objects.count())
        self.client.force_login(self.requester)
        self.client.post(reverse('assistant'), {'question': 'Request the projector for me now'})
        response = self.client.get(reverse('assistant'))
        self.assertEqual((AssetEvent.objects.count(), ResourceRequest.objects.count()), before)
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)
        self.assertContains(response, 'Request this item')  # a link to the normal form

    def test_falls_back_when_provider_fails(self):
        from ai.assistant import AssistantService
        answer = AssistantService(AIGateway(adapter=BrokenAdapter())).ask(self.requester, 'Any stools?')
        self.assertTrue(answer['fallback'])


class OpenNeedTests(CampusTestCase):
    """UC-05 exception and UC-18 step 5: a request for something not listed."""

    def test_unfilled_request_appears_on_wanted_list_for_custodians_only(self):
        self.make_request(title='Microscope for biology lab')
        self.client.force_login(self.custodian)
        self.assertContains(self.client.get(reverse('wanted')), 'Microscope for biology lab')
        self.client.force_login(self.approver)
        self.assertContains(self.client.get(reverse('wanted')), 'Microscope for biology lab')
        self.client.force_login(self.requester)
        self.assertEqual(self.client.get(reverse('wanted')).status_code, 403)

    def test_request_with_item_held_leaves_the_wanted_list(self):
        resource_request = self.make_request(title='Chairs for a lab')
        ExchangeService.reserve(resource_request, self.make_asset().pk, 1, self.requester)
        self.assertEqual(ExchangeService.open_needs(), [])

    def test_publishing_a_fitting_item_notifies_the_requester(self):
        resource_request = self.make_request(title='Chairs for a lab', category=self.chairs)
        before = self.requester.notifications.count()
        self.make_asset('Lab stool')
        newest = self.requester.notifications.first()
        self.assertEqual(self.requester.notifications.count(), before + 1)
        self.assertIn(resource_request.code, newest.message)
        self.assertIn('Lab stool', newest.message)

    def test_no_message_when_the_item_does_not_fit(self):
        self.make_request(title='Chairs for a lab', category=self.chairs, min_condition='GOOD')
        before = self.requester.notifications.count()
        self.make_asset('Dell desktop', category=self.desktops)
        self.make_asset('Old stool', condition='POOR')
        own = CatalogService.register(
            self.cs_custodian, name='CS stool', description='', category=self.chairs,
            condition='GOOD', quantity=1)
        CatalogService.publish(own, self.cs_custodian)  # same department as the requester
        self.assertEqual(self.requester.notifications.count(), before)


class MovementTests(CampusTestCase):
    """Who took which assets: by department and by person."""

    def setUp(self):
        self.lib = Department.objects.create(code='LIB', name='Library')
        self.lib_requester = User.objects.create_user(
            'libby', password='pw', role=User.Role.REQUESTER, department=self.lib)
        self.run_to_receipt(self.make_asset('Lab stool', quantity=3, replacement_unit_cost=Decimal('200')),
                            self.make_request(quantity=3))
        self.run_to_receipt(self.make_asset('Office chair', quantity=2), self.make_request(quantity=2))
        self.run_to_receipt(self.make_asset('Desk'), self.make_request(self.lib_requester))
        # A CS item going to the library, which the ENG custodian must not see.
        own = CatalogService.register(self.cs_custodian, name='CS monitor', description='',
                                      category=self.chairs, condition='GOOD', quantity=1)
        CatalogService.publish(own, self.cs_custodian)
        reservation = ExchangeService.reserve(self.make_request(self.lib_requester), own.pk, 1, self.lib_requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.cs_custodian)
        ExchangeService.receive(reservation.transfer, self.lib_requester)

    def page(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('movements'), params)

    def test_admin_sees_totals_by_department_and_person(self):
        context = self.page(self.admin).context
        self.assertEqual(context['totals']['units'], 7)
        self.assertEqual(context['totals']['transfers'], 4)
        by_department = {d['to_department__name']: d['units'] for d in context['by_department']}
        self.assertEqual(by_department, {'Computer Science': 5, 'Library': 2})
        by_person = {p['received_by__username']: p['units'] for p in context['by_person']}
        self.assertEqual(by_person, {'requester': 5, 'libby': 2})
        self.assertEqual(context['totals']['value'], Decimal('600'))

    def test_custodian_sees_only_items_from_their_department(self):
        context = self.page(self.custodian).context
        self.assertEqual(context['totals']['units'], 6)
        self.assertNotIn('CS monitor', [t.reservation.asset.name for t in context['rows']])

    def test_filters_by_department_and_person(self):
        self.assertEqual(self.page(self.admin, department=self.lib.pk).context['totals']['units'], 2)
        self.assertEqual(self.page(self.admin, person=self.requester.pk).context['totals']['units'], 5)

    def test_requester_cannot_open_it(self):
        self.assertEqual(self.page(self.requester).status_code, 403)

    def test_only_confirmed_hand_overs_count(self):
        asset = self.make_asset('In transit stool')
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        self.assertEqual(self.page(self.admin).context['totals']['units'], 7)

    def test_csv_export(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('movements'), {'format': 'csv', 'department': self.lib.pk})
        lines = response.content.decode('utf-8-sig').strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('taken_by', lines[0])
        self.assertIn('Library', lines[1])


class LastItemPageTests(CampusTestCase):
    """Two people press "Request this item" on the same last item."""

    def test_second_person_gets_a_clear_message(self):
        asset = self.make_asset('Lab stool')
        url = reverse('request_create') + f'?asset={asset.pk}'
        data = {'title': 'Lab stool', 'quantity': 1, 'min_condition': 'FAIR', 'category': self.chairs.pk}
        self.client.force_login(self.requester)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.client.force_login(self.other_requester)
        response = self.client.post(url, data, follow=True)
        self.assertContains(response, 'is no longer available')
        asset.refresh_from_db()
        self.assertEqual(asset.reservations.count(), 1)

    def test_race_after_form_opened_keeps_the_request_open(self):
        asset = self.make_asset('Lab stool')
        # The page still shows it available, but its stock went in the meantime.
        Asset.objects.filter(pk=asset.pk).update(quantity=0)
        self.client.force_login(self.requester)
        response = self.client.post(reverse('request_create') + f'?asset={asset.pk}',
                                    {'title': 'Stool', 'quantity': 1, 'min_condition': 'FAIR'}, follow=True)
        self.assertContains(response, 'stays open')


class NoLockDuringAICallTests(CampusTestCase):
    """A slow AI answer must never hold the database locked (SQLite "database is locked")."""

    class SpyAdapter(ClassicAdapter):
        name, model_id = 'spy', 'spy-model'

        def __init__(self, checks):
            self.checks = checks

        def _check(self):
            from django.db import connection
            # Tests run inside one outer transaction, so count blocks above that one.
            self.checks.append(len(connection.savepoint_ids))

        def relevance(self, need, candidates):
            self._check()
            return super().relevance(need, candidates)

        def advise(self, facts):
            self._check()
            return super().advise(facts)

        def summarise(self, snapshot):
            self._check()
            return super().summarise(snapshot)

    def test_matching_advice_and_summary_call_the_ai_outside_a_transaction(self):
        from ai.advice import AdviceService
        from django.db import connection
        baseline = len(connection.savepoint_ids)  # the test's own wrapping transactions
        checks = []
        gateway = AIGateway(adapter=self.SpyAdapter(checks))
        asset = self.make_asset('Lab stool')
        MatchingService(gateway).find_matches(self.make_request(), self.requester)
        AdviceService(gateway).advise(asset, self.custodian)
        ReportService(gateway).generate(timezone.localdate(), timezone.localdate(), self.admin)
        self.assertEqual(checks, [baseline] * 3)


class ReportCoverageTests(CampusTestCase):
    """C9: the table lists every request, including ones still waiting for an item."""

    def test_requests_without_an_item_are_in_the_report_and_csv(self):
        self.make_request(title='Microscope nobody has')
        reserved = self.make_request(title='Chairs for a lab')
        ExchangeService.reserve(reserved, self.make_asset().pk, 1, self.requester)
        self.client.force_login(self.approver)
        page = self.client.get(reverse('report'))
        self.assertContains(page, 'Microscope nobody has')
        self.assertContains(page, 'No item reserved yet')
        self.assertContains(page, 'Waiting for approval')
        lines = self.client.get(reverse('report'), {'format': 'csv'}).content.decode('utf-8-sig').strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(any('Microscope nobody has' in line for line in lines))
