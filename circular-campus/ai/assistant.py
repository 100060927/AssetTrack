"""AI feature 4: the assistant.

Our code fetches the data the user is allowed to see, and only that data is
given to the AI. The assistant answers; it never changes anything. To act, the
user clicks a normal button and confirms on the normal form.
"""
from accounts.permissions import require
from catalog.models import Asset

from .gateway import AIGateway, _words

MAX_ITEMS = 15
# Words that carry no search meaning in a question.
QUESTION_WORDS = {
    'any', 'there', 'have', 'has', 'do', 'doe', 'you', 'what', 'which', 'show', 'me', 'list',
    'find', 'available', 'item', 'surplu', 'how', 'many', 'much', 'department', 'can', 'get',
    'looking', 'want', 'please', 'from', 'at', 'it', 'that', 'thi', 'now', 'left', 'stock',
    'request', 'notification', 'statu', 'unread', 'hello', 'hi', 'about', 'tell', 'give',
}


class AssistantService:
    def __init__(self, gateway=None):
        self.gateway = gateway or AIGateway()

    @staticmethod
    def retrieve_items(question):
        """Available assets that contain every meaningful word of the question."""
        wanted = _words(question) - QUESTION_WORDS
        if not wanted:
            return []
        matches = []
        for asset in Asset.objects.filter(
                state=Asset.State.AVAILABLE, quantity__gt=0).select_related('category', 'department'):
            have = _words(f'{asset.name} {asset.description} {asset.tags} {asset.category.name} '
                          f'{asset.department.name} {asset.department.code}')
            if wanted <= have:
                matches.append(asset)
        return matches[:MAX_ITEMS]

    def ask(self, user, question):
        require(user, 'assistant.use')
        question = question.strip()[:300]
        context = {
            'items': [{
                'id': asset.pk, 'name': asset.name, 'category': asset.category.name,
                'condition': asset.get_condition_display(),
                'department': asset.department.name, 'quantity': asset.quantity,
            } for asset in self.retrieve_items(question)],
            # Only the asker's own requests: the scope the permission rules allow.
            'requests': [{'code': r.code, 'title': r.title, 'state': r.get_state_display()}
                         for r in user.requests.all()[:10]],
            'unread_notifications': user.notifications.filter(read=False).count(),
        }
        run = self.gateway.converse(question, context, user)
        return {
            'question': question,
            'answer': run.output['answer'],
            'item_ids': run.output['item_ids'][:MAX_ITEMS],
            'model': run.model_id,
            'fallback': run.used_fallback,
        }
