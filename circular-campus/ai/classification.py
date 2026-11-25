"""AI feature 1: classification and tagging at registration.

The AI suggests a category, condition and tags from the description. The
custodian accepts or edits the suggestion before anything is saved.
"""
from accounts.permissions import require
from catalog.models import Asset, Category

from .gateway import AIGateway
from .models import AIRun


class ClassificationService:
    def __init__(self, gateway=None):
        self.gateway = gateway or AIGateway()

    def suggest(self, user, name, description):
        require(user, 'asset.register')
        categories = list(Category.objects.values_list('name', flat=True))
        # SEC-7: only the item text and the category names are sent.
        run = self.gateway.classify({'name': name, 'description': description}, categories, user)
        output = run.output
        category = Category.objects.filter(name=output['category']).first()
        return {
            'run': run,
            'category': category,
            'condition': output['condition'],
            'condition_label': Asset.Condition(output['condition']).label,
            'tags': ', '.join(output['tags']),
            'confidence': output['confidence'],
        }

    @staticmethod
    def review_note(run_id, user, category, condition):
        """History note saying whether the custodian kept or changed the suggestion."""
        if not str(run_id or '').isdigit():
            return ''  # the custodian filled the form without asking the AI
        run = AIRun.objects.filter(pk=run_id, task=AIRun.Task.CLASSIFY, created_by=user).first()
        if run is None:
            return ''
        same = (run.output['category'] == category.name and run.output['condition'] == condition)
        verdict = 'accepted' if same else 'edited'
        return f'AI classification {verdict} by the custodian (run {run.pk}, {run.model_id})'
