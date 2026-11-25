"""Checks for UC-09/21, UC-15, UC-18, UC-19, UC-20, UC-26 and UC-30."""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, User
from audit import backup
from audit.models import AuditEntry
from catalog.models import Asset, Category
from catalog.services import CatalogService
from core.errors import DomainError
from exchange.models import ApprovalCase, Reservation, ResourceRequest, Transfer
from exchange.services import ExchangeService
from exchange.tests import CampusTestCase

from .models import EndOfLifeProposal, MaintenanceTask
from .services import LifeCycleService


class MaintenanceSetup(CampusTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.fixer = User.objects.create_user('fixer', password='pw', role=User.Role.MAINTENANCE)


class DefectAndRepairTests(MaintenanceSetup):
    """UC-21 report a defect, UC-09 inspection and repair."""

    def test_unsafe_report_blocks_item_and_clearance_puts_it_back(self):
        asset = self.make_asset()
        task = LifeCycleService.report_defect(asset, self.requester, 'Leg is cracked', unsafe=True)
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.BLOCKED)
        self.assertEqual(task.blocked_from, Asset.State.AVAILABLE)
        self.assertNotIn(asset, CatalogService.search())
        LifeCycleService.record_inspection(task, self.fixer, 'SAFE', 'Leg replaced', 'New leg', Decimal('40'))
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)
        self.assertEqual(asset.events.last().action, 'Cleared by maintenance')

    def test_minor_defect_does_not_block(self):
        asset = self.make_asset()
        LifeCycleService.report_defect(asset, self.requester, 'Scratched seat')
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)

    def test_second_report_joins_the_open_task(self):
        asset = self.make_asset()
        first = LifeCycleService.report_defect(asset, self.requester, 'Wobbles')
        second = LifeCycleService.report_defect(asset, self.other_requester, 'Really wobbles', unsafe=True)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(MaintenanceTask.objects.count(), 1)
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.BLOCKED)

    def test_beyond_repair_stays_blocked_and_condition_updates(self):
        asset = self.make_asset()
        task = LifeCycleService.report_defect(asset, self.requester, 'Frame bent')
        LifeCycleService.record_inspection(task, self.fixer, 'BEYOND', 'Frame cannot be fixed', condition='POOR')
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.BLOCKED)
        self.assertEqual(asset.condition, 'POOR')

    def test_only_maintenance_staff_record_inspections(self):
        task = LifeCycleService.report_defect(self.make_asset(), self.requester, 'Broken')
        with self.assertRaises(PermissionDenied):
            LifeCycleService.record_inspection(task, self.custodian, 'SAFE', 'Looks fine')

    def test_unsafe_held_item_cannot_be_dispatched_and_is_blocked_when_released(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        LifeCycleService.report_defect(asset, self.custodian, 'Sparks from the motor', unsafe=True)
        with self.assertRaises(DomainError):
            ExchangeService.dispatch(reservation.transfer, self.custodian)
        ExchangeService.cancel(reservation.request, self.requester)
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.BLOCKED)

    def test_report_defect_page(self):
        asset = self.make_asset()
        self.client.force_login(self.requester)
        self.client.post(reverse('defect_report', args=[asset.pk]),
                         {'description': 'Wheel missing', 'unsafe': 'on'})
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.BLOCKED)
        self.client.force_login(self.fixer)
        task = MaintenanceTask.objects.get()
        response = self.client.post(reverse('maintenance_detail', args=[task.pk]), {
            'findings': 'Fitted a wheel', 'outcome': 'SAFE', 'repair_cost': '15'})
        self.assertEqual(response.status_code, 302)
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)


class EditAssetTests(CampusTestCase):
    """UC-15."""

    def test_edit_records_old_and_new_values(self):
        asset = self.make_asset(condition='GOOD')
        CatalogService.edit(asset, self.custodian, {'condition': 'FAIR', 'location': 'Room 2'})
        asset.refresh_from_db()
        self.assertEqual(asset.condition, 'FAIR')
        note = asset.events.last().note
        self.assertIn('Good → Fair', note)
        self.assertIn('Room 2', note)
        self.assertTrue(AuditEntry.objects.filter(action='Edited').exists())

    def test_quantity_change_needs_reason(self):
        asset = self.make_asset(quantity=4)
        with self.assertRaises(DomainError):
            CatalogService.edit(asset, self.custodian, {'quantity': 3})
        CatalogService.edit(asset, self.custodian, {'quantity': 3}, reason='Recount')
        asset.refresh_from_db()
        self.assertEqual(asset.quantity, 3)

    def test_cannot_edit_held_item_or_other_department(self):
        asset = self.make_asset()
        with self.assertRaises(PermissionDenied):
            CatalogService.edit(asset, self.cs_custodian, {'name': 'Mine now'})
        ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        with self.assertRaises(DomainError):
            CatalogService.edit(Asset.objects.get(pk=asset.pk), self.custodian, {'name': 'Renamed'})

    def test_edit_page(self):
        asset = self.make_asset()
        self.client.force_login(self.custodian)
        response = self.client.post(reverse('asset_edit', args=[asset.pk]), {
            'name': 'Tall lab stool', 'category': self.chairs.pk, 'condition': 'GOOD', 'quantity': 1})
        self.assertRedirects(response, reverse('asset_detail', args=[asset.pk]))
        self.client.force_login(self.requester)
        self.assertEqual(self.client.get(reverse('asset_edit', args=[asset.pk])).status_code, 403)


class HoldExpiryTests(CampusTestCase):
    """UC-18."""

    def test_expired_hold_is_released_and_request_reopens(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        later = timezone.now() + timedelta(days=8)
        result = ExchangeService.process_expiry(later)
        self.assertEqual(result['expired'], 1)
        asset.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.AVAILABLE)
        self.assertEqual(reservation.state, Reservation.State.RELEASED)
        self.assertEqual(reservation.request.state, ResourceRequest.State.SUBMITTED)
        self.assertEqual(reservation.approval_case.state, ApprovalCase.State.WITHDRAWN)
        self.assertIsNone(asset.events.last().actor)  # done by the system
        self.assertEqual(ExchangeService.process_expiry(later)['expired'], 0)  # runs safely twice

    def test_approved_hold_expires_and_transfer_is_cancelled(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.process_expiry(timezone.now() + timedelta(days=8))
        reservation.transfer.refresh_from_db()
        self.assertEqual(reservation.transfer.state, Transfer.State.CANCELLED)

    def test_dispatched_item_never_expires(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        ExchangeService.dispatch(reservation.transfer, self.custodian)
        self.assertEqual(ExchangeService.process_expiry(timezone.now() + timedelta(days=30))['expired'], 0)

    def test_warning_sent_once_and_extension_moves_the_date(self):
        reservation = ExchangeService.reserve(self.make_request(), self.make_asset().pk, 1, self.requester)
        almost = reservation.expires_at - timedelta(hours=2)
        self.assertEqual(ExchangeService.process_expiry(almost)['warned'], 1)
        self.assertEqual(ExchangeService.process_expiry(almost)['warned'], 0)
        old = reservation.expires_at
        ExchangeService.extend_hold(reservation, self.approver, 3, 'Waiting for transport')
        reservation.refresh_from_db()
        self.assertGreater(reservation.expires_at, old)
        with self.assertRaises(PermissionDenied):
            ExchangeService.extend_hold(reservation, self.requester, 3, 'Please')

    def test_old_approval_gets_one_reminder(self):
        reservation = ExchangeService.reserve(self.make_request(), self.make_asset().pk, 1, self.requester)
        ApprovalCase.objects.filter(pk=reservation.approval_case.pk).update(
            created_at=timezone.now() - timedelta(days=4))
        self.assertEqual(ExchangeService.process_expiry()['reminded'], 1)
        self.assertEqual(ExchangeService.process_expiry()['reminded'], 0)

    def test_command_with_pretend_days(self):
        ExchangeService.reserve(self.make_request(), self.make_asset().pk, 1, self.requester)
        call_command('process_expiry', pretend_days=8, stdout=open('/dev/null', 'w'))
        self.assertEqual(Reservation.objects.get().state, Reservation.State.RELEASED)


class DisputeTests(CampusTestCase):
    """UC-19."""

    def dispatched(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        return ExchangeService.dispatch(reservation.transfer, self.custodian)

    def test_problem_stops_receipt_and_keeps_owner(self):
        transfer = self.dispatched()
        ExchangeService.report_problem(transfer, self.requester, 'WRONG', 'A desk, not a stool')
        with self.assertRaises(DomainError):
            ExchangeService.receive(transfer, self.requester)
        asset = transfer.reservation.asset
        asset.refresh_from_db()
        self.assertEqual(asset.owner, self.custodian)
        with self.assertRaises(DomainError):
            ExchangeService.cancel(transfer.reservation.request, self.requester)

    def test_requester_cannot_resolve_own_dispute(self):
        transfer = self.dispatched()
        ExchangeService.report_problem(transfer, self.requester, 'WRONG', 'Wrong one')
        with self.assertRaises(PermissionDenied):
            ExchangeService.resolve_dispute(transfer, self.requester, 'RECEIVED', 'Fine')

    def test_resolved_as_received_moves_ownership_to_requester(self):
        transfer = self.dispatched()
        ExchangeService.report_problem(transfer, self.requester, 'MISSING', 'Box not found')
        ExchangeService.resolve_dispute(transfer, self.approver, 'RECEIVED', 'Found at reception')
        asset = Asset.objects.get(pk=transfer.reservation.asset_id)
        self.assertEqual(asset.state, Asset.State.IN_USE)
        self.assertEqual(asset.owner, self.requester)
        self.assertEqual(transfer.reservation.request.__class__.objects.get(
            pk=transfer.reservation.request_id).state, ResourceRequest.State.FULFILLED)

    def test_damaged_return_goes_to_maintenance_and_request_reopens(self):
        transfer = self.dispatched()
        ExchangeService.report_problem(transfer, self.requester, 'DAMAGED', 'Seat torn')
        ExchangeService.resolve_dispute(transfer, self.approver, 'RETURNED', 'Sent back')
        asset = Asset.objects.get(pk=transfer.reservation.asset_id)
        self.assertEqual(asset.state, Asset.State.BLOCKED)
        self.assertTrue(MaintenanceTask.objects.filter(asset=asset, unsafe=True).exists())
        request = ResourceRequest.objects.get(pk=transfer.reservation.request_id)
        self.assertEqual(request.state, ResourceRequest.State.SUBMITTED)
        self.assertTrue(request.is_open_for_matching)

    def test_lost_item_is_written_off(self):
        transfer = self.dispatched()
        ExchangeService.report_problem(transfer, self.requester, 'MISSING', 'Never arrived')
        ExchangeService.resolve_dispute(transfer, self.approver, 'LOST', 'Not found anywhere')
        self.assertEqual(Asset.objects.get(pk=transfer.reservation.asset_id).state, Asset.State.TERMINAL)


class EndOfLifeTests(MaintenanceSetup):
    """UC-20."""

    def test_full_cycle_propose_approve_complete(self):
        asset = self.make_asset()
        proposal = LifeCycleService.propose(asset, self.custodian, 'DONATE', 'Old but works; nobody asked')
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.RECORDED)  # off the marketplace during review
        LifeCycleService.decide(proposal, self.approver, True)
        with self.assertRaises(DomainError):
            LifeCycleService.complete(proposal, self.custodian, '', 'RCPT-1')  # donation needs a recipient
        LifeCycleService.complete(proposal, self.custodian, 'City school', 'RCPT-1')
        asset.refresh_from_db()
        self.assertEqual(asset.state, Asset.State.TERMINAL)
        self.assertEqual(asset.events.last().action, 'Donated')

    def test_proposer_cannot_approve_own_proposal(self):
        proposal = LifeCycleService.propose(self.make_asset(), self.approver, 'RECYCLE', 'Broken')
        with self.assertRaises(PermissionDenied):
            LifeCycleService.decide(proposal, self.approver, True)

    def test_maintenance_and_requesters_cannot_propose(self):
        asset = self.make_asset()
        for user in (self.fixer, self.requester):
            with self.assertRaises(PermissionDenied):
                LifeCycleService.propose(asset, user, 'DISPOSE', 'Junk')

    def test_cannot_complete_before_approval_or_propose_held_item(self):
        asset = self.make_asset()
        proposal = LifeCycleService.propose(asset, self.custodian, 'RECYCLE', 'Beyond repair')
        with self.assertRaises(DomainError):
            LifeCycleService.complete(proposal, self.custodian, '', 'CERT-9')
        other = self.make_asset('Desk')
        ExchangeService.reserve(self.make_request(), other.pk, 1, self.requester)
        with self.assertRaises(DomainError):
            LifeCycleService.propose(Asset.objects.get(pk=other.pk), self.custodian, 'DISPOSE', 'x')

    def test_rejection_needs_reason(self):
        proposal = LifeCycleService.propose(self.make_asset(), self.custodian, 'DISPOSE', 'Old')
        with self.assertRaises(DomainError):
            LifeCycleService.decide(proposal, self.approver, False, '')
        LifeCycleService.decide(proposal, self.approver, False, 'Repair it first')
        self.assertEqual(EndOfLifeProposal.objects.get().state, EndOfLifeProposal.State.REJECTED)


class AuditLogTests(MaintenanceSetup):
    """UC-26."""

    def test_changes_appear_in_one_log_and_cannot_be_changed(self):
        asset = self.make_asset()
        self.make_request()
        actions = set(AuditEntry.objects.values_list('action', flat=True))
        self.assertTrue({'Registered', 'Published', 'Request submitted'} <= actions)
        entry = AuditEntry.objects.first()
        with self.assertRaises(DomainError):
            entry.save()
        with self.assertRaises(DomainError):
            entry.delete()

    def test_page_filters_csv_and_permissions(self):
        self.make_asset('Unique microscope')
        self.client.force_login(self.approver)
        response = self.client.get(reverse('audit_log'), {'q': 'microscope'})
        self.assertContains(response, 'Unique microscope')
        csv = self.client.get(reverse('audit_log'), {'format': 'csv'})
        self.assertEqual(csv['Content-Type'], 'text/csv')
        for user in (self.requester, self.custodian, self.fixer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('audit_log')).status_code, 403)


class BackupTests(TransactionTestCase):
    """UC-30. A real flush and reload, so it runs outside a test transaction."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.override = override_settings(BACKUP_DIR=self.folder, AI_PROVIDER='classic')
        self.override.enable()
        self.addCleanup(self.override.disable)
        dept = Department.objects.create(code='ENG', name='Engineering')
        self.admin = User.objects.create_user('boss', password='pw', role=User.Role.ADMIN)
        self.custodian = User.objects.create_user('c', password='pw', role=User.Role.CUSTODIAN, department=dept)
        self.category = Category.objects.create(name='Chairs')

    def register(self, name):
        return CatalogService.register(self.custodian, name=name, category=self.category,
                                       condition='GOOD', quantity=1)

    def test_backup_then_restore_brings_back_old_data(self):
        self.register('Before backup')
        path, rows = backup.create_backup(self.admin)
        self.assertGreater(rows, 0)
        self.register('After backup')
        self.assertEqual(Asset.objects.count(), 2)
        safety, check = backup.restore_backup(self.admin, path.name, 'RESTORE')
        self.assertEqual(list(Asset.objects.values_list('name', flat=True)), ['Before backup'])
        self.assertTrue(all(a == b for a, b in check.values()))
        self.assertTrue(safety.exists())
        self.assertTrue(AuditEntry.objects.filter(action='Database restored').exists())

    def test_restore_needs_typed_confirmation_and_admin(self):
        path, _ = backup.create_backup(self.admin)
        with self.assertRaises(DomainError):
            backup.restore_backup(self.admin, path.name, 'yes')
        with self.assertRaises(PermissionDenied):
            backup.create_backup(self.custodian)
        with self.assertRaises(DomainError):
            backup.restore_backup(self.admin, '../db.sqlite3', 'RESTORE')

    def test_backup_page_only_for_admin(self):
        self.client.force_login(self.custodian)
        self.assertEqual(self.client.get(reverse('backup')).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(reverse('backup_create'))
        self.assertEqual(len(backup.list_backups()), 1)


class CustodianSeesMaintenanceTests(MaintenanceSetup):
    def test_custodian_told_even_when_item_is_in_use_by_someone_else(self):
        from notifications.models import Notification
        asset = self.make_asset()
        self.run_to_receipt(asset, self.make_request())  # now CS, owned by the requester
        LifeCycleService.report_defect(Asset.objects.get(pk=asset.pk), self.requester, 'Broken', unsafe=True)
        self.assertTrue(Notification.objects.filter(recipient=self.cs_custodian, message__contains='UNSAFE').exists())
        self.client.force_login(self.cs_custodian)
        self.assertContains(self.client.get(reverse('maintenance_list')), asset.tag)

    def test_custodian_sees_what_they_reported_on_other_departments(self):
        asset = self.make_asset()  # Engineering item
        LifeCycleService.report_defect(asset, self.cs_custodian, 'Loose screw')
        self.client.force_login(self.cs_custodian)
        self.assertContains(self.client.get(reverse('maintenance_list')), asset.tag)


class MyItemsTests(CampusTestCase):
    def test_receiver_sees_own_items_only_and_can_offer_back(self):
        from notifications.models import Notification
        asset = self.make_asset('Projector')
        self.run_to_receipt(asset, self.make_request())
        self.client.force_login(self.requester)
        self.assertContains(self.client.get(reverse('my_items')), 'Projector')
        self.client.force_login(self.other_requester)
        self.assertNotContains(self.client.get(reverse('my_items')), 'Projector')
        self.assertEqual(self.client.get(reverse('asset_detail', args=[asset.pk])).status_code, 200)  # same department
        with self.assertRaises(PermissionDenied):
            CatalogService.offer_back(Asset.objects.get(pk=asset.pk), self.other_requester)
        CatalogService.offer_back(Asset.objects.get(pk=asset.pk), self.requester, 'Project finished')
        self.assertTrue(Notification.objects.filter(recipient=self.cs_custodian, message__contains='no longer needs').exists())
        CatalogService.publish(Asset.objects.get(pk=asset.pk), self.cs_custodian)  # custodian declares surplus
        self.assertEqual(Asset.objects.get(pk=asset.pk).state, Asset.State.AVAILABLE)

    def test_admin_has_no_my_items(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('my_items')).status_code, 403)


class AllAssetsTests(CampusTestCase):
    def test_approver_sees_every_department_with_details(self):
        self.make_asset('Engineering stool', replacement_unit_cost=Decimal('120'))
        CatalogService.register(self.cs_custodian, name='CS monitor', category=self.desktops,
                                condition='GOOD', quantity=2)
        self.client.force_login(self.approver)
        page = self.client.get(reverse('all_assets'))
        self.assertContains(page, 'Engineering stool')
        self.assertContains(page, 'CS monitor')  # even though not published
        self.assertContains(page, '120.00')
        only_cs = self.client.get(reverse('all_assets'), {'department': self.cs.pk})
        self.assertNotContains(only_cs, 'Engineering stool')
        csv = self.client.get(reverse('all_assets'), {'format': 'csv'})
        self.assertIn(b'CS monitor', csv.content)

    def test_others_cannot_open_it(self):
        for user in (self.requester, self.custodian):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('all_assets')).status_code, 403)


class BeyondRepairWhileReservedTests(MaintenanceSetup):
    def test_beyond_repair_item_cannot_be_sent_and_is_blocked_when_released(self):
        asset = self.make_asset()
        reservation = ExchangeService.reserve(self.make_request(), asset.pk, 1, self.requester)
        ExchangeService.decide(reservation.approval_case, self.approver2, True)
        task = LifeCycleService.report_defect(asset, self.custodian, 'Motor burnt')
        LifeCycleService.record_inspection(task, self.fixer, 'BEYOND', 'Motor cannot be fixed')
        with self.assertRaises(DomainError):
            ExchangeService.dispatch(reservation.transfer, self.custodian)
        ExchangeService.cancel(reservation.request, self.requester)
        self.assertEqual(Asset.objects.get(pk=asset.pk).state, Asset.State.BLOCKED)


class RepairCostVisibilityTests(MaintenanceSetup):
    def test_requester_never_sees_repair_cost(self):
        asset = self.make_asset()
        task = LifeCycleService.report_defect(asset, self.requester, 'Broken leg')
        LifeCycleService.record_inspection(task, self.fixer, 'SAFE', 'Fixed', 'New leg', Decimal('77.50'))
        self.client.force_login(self.requester)
        self.assertNotContains(self.client.get(reverse('asset_detail', args=[asset.pk])), '77.50')
        self.assertNotContains(self.client.get(reverse('maintenance_detail', args=[task.pk])), '77.50')
        self.client.force_login(self.custodian)
        self.assertContains(self.client.get(reverse('asset_detail', args=[asset.pk])), '77.50')


class DemoPretendDaysTests(CampusTestCase):
    @override_settings(DEBUG=True)
    def test_holds_page_can_pretend_days_in_demo_mode(self):
        ExchangeService.reserve(self.make_request(), self.make_asset().pk, 1, self.requester)
        self.client.force_login(self.approver)
        self.client.post(reverse('holds_run_expiry'), {'pretend_days': '8'})
        self.assertEqual(Reservation.objects.get().state, Reservation.State.RELEASED)

    @override_settings(DEBUG=False)
    def test_pretend_days_ignored_outside_demo_mode(self):
        ExchangeService.reserve(self.make_request(), self.make_asset().pk, 1, self.requester)
        self.client.force_login(self.approver)
        self.client.post(reverse('holds_run_expiry'), {'pretend_days': '8'})
        self.assertEqual(Reservation.objects.get().state, Reservation.State.ACTIVE)


class ReporterCanStillSeeItemTests(MaintenanceSetup):
    def test_reporter_returns_to_item_page_after_unsafe_report(self):
        asset = self.make_asset()  # Engineering item; the requester is in CS
        self.client.force_login(self.requester)
        response = self.client.post(reverse('defect_report', args=[asset.pk]),
                                    {'description': 'Sharp edge', 'unsafe': 'on'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'was sent to maintenance')
        self.client.force_login(self.other_requester)  # someone who did not report it
        self.assertEqual(self.client.get(reverse('asset_detail', args=[asset.pk])).status_code, 403)
