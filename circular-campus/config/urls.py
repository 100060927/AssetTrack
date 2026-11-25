from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from accounts import views as accounts
from audit import views as audit
from catalog import views as catalog
from lifecycle import views as lifecycle
from exchange import views as exchange
from notifications import views as notifications
from reports import views as reports

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('password/', accounts.password_change, name='password_change'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('', exchange.home, name='home'),

    path('marketplace/', catalog.marketplace, name='marketplace'),
    path('assets/', catalog.inventory, name='inventory'),
    path('all-assets/', catalog.all_assets, name='all_assets'),
    path('my-items/', catalog.my_items, name='my_items'),
    path('assets/new/', catalog.asset_create, name='asset_create'),
    path('assets/<int:pk>/', catalog.asset_detail, name='asset_detail'),
    path('assets/<int:pk>/edit/', catalog.asset_edit, name='asset_edit'),
    path('assets/<int:pk>/report-defect/', lifecycle.defect_report, name='defect_report'),
    path('assets/<int:pk>/end-of-life/', lifecycle.eol_propose, name='eol_propose'),
    path('assets/<int:pk>/<str:action>/', catalog.asset_action, name='asset_action'),

    path('requests/', exchange.request_list, name='request_list'),
    path('requests/new/', exchange.request_create, name='request_create'),
    path('requests/<int:pk>/', exchange.request_detail, name='request_detail'),
    path('requests/<int:pk>/matches/', exchange.request_find_matches, name='request_find_matches'),
    path('requests/<int:pk>/cancel/', exchange.request_cancel, name='request_cancel'),
    path('recommendations/<int:pk>/decide/', exchange.recommendation_decide, name='recommendation_decide'),

    path('wanted/', exchange.wanted, name='wanted'),
    path('approvals/', exchange.approval_queue, name='approval_queue'),
    path('approvals/<int:pk>/decide/', exchange.approval_decide, name='approval_decide'),
    path('transfers/', exchange.transfer_queue, name='transfer_queue'),
    path('transfers/<int:pk>/dispatch/', exchange.transfer_dispatch, name='transfer_dispatch'),
    path('transfers/<int:pk>/receive/', exchange.transfer_receive, name='transfer_receive'),

    path('holds/', exchange.holds, name='holds'),
    path('holds/run-expiry/', exchange.holds_run_expiry, name='holds_run_expiry'),
    path('holds/<int:pk>/extend/', exchange.hold_extend, name='hold_extend'),
    path('transfers/<int:pk>/problem/', exchange.transfer_problem, name='transfer_problem'),
    path('disputes/', exchange.dispute_list, name='dispute_list'),
    path('disputes/<int:pk>/resolve/', exchange.dispute_resolve, name='dispute_resolve'),

    path('maintenance/', lifecycle.maintenance_list, name='maintenance_list'),
    path('maintenance/<int:pk>/', lifecycle.maintenance_detail, name='maintenance_detail'),
    path('end-of-life/', lifecycle.eol_list, name='eol_list'),
    path('end-of-life/<int:pk>/decide/', lifecycle.eol_decide, name='eol_decide'),
    path('end-of-life/<int:pk>/complete/', lifecycle.eol_complete, name='eol_complete'),

    path('audit/', audit.audit_log, name='audit_log'),
    path('backup/', audit.backup_page, name='backup'),
    path('backup/create/', audit.backup_create, name='backup_create'),
    path('backup/restore/', audit.backup_restore, name='backup_restore'),
    path('backup/download/<str:name>/', audit.backup_download, name='backup_download'),

    path('assistant/', exchange.assistant, name='assistant'),

    path('notifications/', notifications.notification_list, name='notifications'),
    path('notifications/read-all/', notifications.notification_read_all, name='notifications_read_all'),
    path('notifications/<int:pk>/open/', notifications.notification_open, name='notification_open'),

    path('dashboard/', reports.dashboard, name='dashboard'),
    path('reports/', reports.report, name='report'),
    path('movements/', reports.movements, name='movements'),
    path('reports/summaries/', reports.summaries, name='summaries'),
    path('reports/summaries/<int:pk>/', reports.summary_detail, name='summary_detail'),
    path('reports/summaries/<int:pk>/update/', reports.summary_update, name='summary_update'),
]
