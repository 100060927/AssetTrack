"""Loads a labelled fictional dataset (DB-5): departments, users, at least 50
assets and 30 requests. Workflows are run through the real services, so the
history, notifications and stock are exactly what the application produces."""
import random
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Department, User
from catalog.models import Asset, Category
from catalog.services import CatalogService
from exchange.models import ResourceRequest
from exchange.services import ExchangeService

PASSWORD = 'campus123'

DEPARTMENTS = [
    ('ENG', 'Engineering', 'Main Campus', 'Building A'),
    ('CS', 'Computer Science', 'Main Campus', 'Building B'),
    ('BUS', 'Business', 'Main Campus', 'Building C'),
    ('LIB', 'Library', 'Main Campus', 'Building B'),
    ('FAC', 'Facilities', 'North Campus', 'Service Block'),
]

# username, first, last, role, department
USERS = [
    ('admin', 'Aisha', 'Admin', User.Role.ADMIN, None),
    ('layla', 'Layla', 'Hassan', User.Role.APPROVER, 'FAC'),
    ('yousef', 'Yousef', 'Khan', User.Role.APPROVER, 'BUS'),
    ('omar', 'Omar', 'Saleh', User.Role.CUSTODIAN, 'ENG'),
    ('noura', 'Noura', 'Ali', User.Role.CUSTODIAN, 'CS'),
    ('khalid', 'Khalid', 'Rashed', User.Role.CUSTODIAN, 'BUS'),
    ('mariam', 'Mariam', 'Zayed', User.Role.CUSTODIAN, 'LIB'),
    ('sara', 'Sara', 'Ahmed', User.Role.REQUESTER, 'CS'),
    ('hamad', 'Hamad', 'Nasser', User.Role.REQUESTER, 'ENG'),
    ('fatima', 'Fatima', 'Saeed', User.Role.REQUESTER, 'BUS'),
    ('ali', 'Ali', 'Mansoor', User.Role.REQUESTER, 'LIB'),
    ('hassan', 'Hassan', 'Obaid', User.Role.MAINTENANCE, 'FAC'),
]

# Only the desktop factors quoted in SRS 6.2 are loaded. Other categories have
# no approved factor, so their carbon figure shows as "not available".
WARM = 'EPA WARM v16 (Dec 2023), Electronics, Exhibit 1-5: desktop CPUs'
CATEGORIES = [
    ('Desktop computers', Decimal('-20.86'), Decimal('0.02'), WARM),
    ('Monitors', None, None, ''),
    ('Laptops', None, None, ''),
    ('Projectors', None, None, ''),
    ('Chairs', None, None, ''),
    ('Desks and tables', None, None, ''),
    ('Lab equipment', None, None, ''),
    ('Printers', None, None, ''),
]

# category, name, description, replacement cost AED, mass kg
ITEMS = [
    ('Desktop computers', 'Dell OptiPlex desktop', 'Core i5, 8 GB RAM, 256 GB SSD, with keyboard and mouse', 2600, 6.5),
    ('Desktop computers', 'HP ProDesk mini PC', 'Core i5, 16 GB RAM, small form factor', 2900, 1.3),
    ('Monitors', 'Dell 24-inch monitor', 'Full HD, HDMI and DisplayPort, height-adjustable stand', 650, 5.2),
    ('Monitors', 'Samsung 27-inch monitor', 'QHD display, small scratch on the bezel', 950, 6.0),
    ('Laptops', 'Lenovo ThinkPad laptop', '14-inch, Core i5, 8 GB RAM, charger included', 3400, 1.6),
    ('Projectors', 'Epson classroom projector', 'XGA, 3,600 lumens, ceiling mount and remote', 1800, 2.7),
    ('Chairs', 'Lab stool', 'Height-adjustable stool with footrest, suitable for lab benches', 180, 4.5),
    ('Chairs', 'Office task chair', 'Mesh back, adjustable arms, on castors', 420, 12.0),
    ('Chairs', 'Stackable lecture chair', 'Plastic seat with writing tablet', 150, 5.0),
    ('Desks and tables', 'Office desk 140 cm', 'Laminate top with cable tray and drawer unit', 700, 38.0),
    ('Desks and tables', 'Folding seminar table', '180 cm folding table for events and exams', 350, 16.0),
    ('Lab equipment', 'Digital oscilloscope', '100 MHz, 2 channels, with probes', 2200, 3.0),
    ('Lab equipment', 'Bench power supply', '0 to 30 V, 5 A, single output', 480, 4.2),
    ('Printers', 'HP LaserJet printer', 'Mono laser, duplex, network ready', 1100, 9.5),
    ('Printers', 'Label printer', 'Thermal label printer for asset tags', 600, None),
]

# requester, title, purpose, category, quantity, minimum condition
NEEDS = [
    ('sara', '4 chairs for a lab', 'Stools or chairs for the new networking lab benches', 'Chairs', 4, 'FAIR'),
    ('hamad', 'Desktop PCs for simulation', 'Run circuit simulation software in the teaching lab', 'Desktop computers', 2, 'GOOD'),
    ('hamad', 'Projector for seminar room', 'Weekly research seminar presentations', 'Projectors', 1, 'FAIR'),
    ('sara', 'Monitors for student project room', 'Second screens for capstone teams', 'Monitors', 3, 'GOOD'),
    ('fatima', 'Office desk for new staff member', 'Desk with drawers for an administrator', 'Desks and tables', 1, 'FAIR'),
    ('fatima', 'Printer for the department office', 'Shared network laser printer', 'Printers', 1, 'FAIR'),
    ('ali', 'Laptops for the help desk', 'Loan laptops for library visitors', 'Laptops', 2, 'GOOD'),
    ('ali', 'Tables for a reading event', 'Folding tables for a book fair', 'Desks and tables', 3, 'FAIR'),
    ('sara', 'Oscilloscope for IoT course', 'Hardware debugging in the embedded systems course', 'Lab equipment', 1, 'GOOD'),
    ('hamad', 'Task chairs for graduate office', 'Comfortable chairs for six research students', 'Chairs', 6, 'FAIR'),
]


class Command(BaseCommand):
    help = 'Load the fictional demonstration dataset.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Load even if data already exists.')

    @transaction.atomic
    def handle(self, *args, **options):
        if Asset.objects.exists() and not options['force']:
            raise CommandError('Data already exists. Use a fresh database or --force.')
        rng = random.Random(336)

        departments = {
            code: Department.objects.get_or_create(
                code=code, defaults={'name': name, 'campus': campus, 'building': building})[0]
            for code, name, campus, building in DEPARTMENTS}
        users = {}
        for username, first, last, role, dept in USERS:
            user, created = User.objects.get_or_create(username=username, defaults={
                'first_name': first, 'last_name': last, 'role': role,
                'department': departments.get(dept),
                'is_staff': role == User.Role.ADMIN, 'is_superuser': role == User.Role.ADMIN})
            if created:
                user.set_password(PASSWORD)
                user.save()
            users[username] = user
        categories = {
            name: Category.objects.get_or_create(name=name, defaults={
                'warm_source_reduction': reduction, 'warm_landfill': landfill,
                'factor_source': source})[0]
            for name, reduction, landfill, source in CATEGORIES}

        # 52 assets across the four custodians; about three quarters published.
        custodians = [users[name] for name in ('omar', 'noura', 'khalid', 'mariam')]
        conditions = ['NEW', 'GOOD', 'GOOD', 'FAIR', 'FAIR', 'POOR']
        assets = []
        for index in range(52):
            category, name, description, cost, mass = ITEMS[index % len(ITEMS)]
            custodian = custodians[index % len(custodians)]
            asset = CatalogService.register(
                custodian, name=name, description=description, category=categories[category],
                condition=rng.choice(conditions), quantity=rng.choice([1, 1, 2, 4, 6]),
                location=f'{custodian.department.building}, store room {rng.randint(1, 4)}',
                replacement_unit_cost=Decimal(cost), unit_mass_kg=None if mass is None else Decimal(str(mass)),
                is_fictional=True)
            if index % 4 != 3:
                CatalogService.publish(asset, custodian)
            assets.append(asset)

        # 30 requests, moved to different stages of the cycle.
        approver, second_approver = users['layla'], users['yousef']
        stages = ['received', 'received', 'received', 'dispatched', 'approved',
                  'reserved', 'rejected', 'submitted', 'submitted', 'cancelled']
        for index in range(30):
            username, title, purpose, category, quantity, minimum = NEEDS[index % len(NEEDS)]
            requester = users[username]
            resource_request = ExchangeService.submit(
                requester, title=title, purpose=purpose, category=categories[category],
                quantity=quantity, min_condition=minimum)
            stage = stages[index % len(stages)]
            if stage == 'submitted':
                continue
            if stage == 'cancelled':
                ExchangeService.cancel(resource_request, requester)
                continue
            asset = next((a for a in Asset.objects.filter(state=Asset.State.AVAILABLE).order_by('id')
                          if a.eligible_for(resource_request)), None)
            if asset is None:
                continue
            reservation = ExchangeService.reserve(
                resource_request, asset.pk, min(quantity, asset.quantity), requester)
            if stage == 'reserved':
                continue
            decider = approver if index % 2 == 0 else second_approver
            if stage == 'rejected':
                ExchangeService.decide(reservation.approval_case, decider, False, 'Needed by the owning department this term')
                continue
            ExchangeService.decide(reservation.approval_case, decider, True)
            if stage == 'approved':
                continue
            transfer = reservation.transfer
            ExchangeService.dispatch(transfer, reservation.asset.owner)
            if stage == 'received':
                ExchangeService.receive(transfer, requester)

        # Example repair jobs so the Maintenance page has something to show.
        from lifecycle.services import LifeCycleService
        by_tag = {a.pk: a for a in Asset.objects.filter(pk__in=[13, 14, 23])}
        if len(by_tag) == 3:
            LifeCycleService.report_defect(
                by_tag[13], users['hamad'], 'Output voltage jumps when the knob is turned. Gave a small shock.',
                unsafe=True)
            LifeCycleService.report_defect(by_tag[14], users['sara'], 'Paper jams on every second page.')
            closed = LifeCycleService.report_defect(
                by_tag[23], users['fatima'], 'Gas lift sinks slowly; seat ends up at the lowest height.', unsafe=True)
            LifeCycleService.record_inspection(
                closed, users['hassan'], 'SAFE', 'Gas cylinder worn out.', 'Fitted a new gas cylinder.',
                Decimal('85.00'))

        self.stdout.write(self.style.SUCCESS(
            f'Loaded {Asset.objects.count()} asset records and '
            f'{ResourceRequest.objects.count()} requests. '
            f'Sign in as sara, omar, layla, hassan or admin with password "{PASSWORD}".'))
