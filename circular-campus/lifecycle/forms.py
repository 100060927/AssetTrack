from django import forms

from catalog.models import Asset

from .models import EndOfLifeProposal, MaintenanceTask


class DefectForm(forms.Form):
    description = forms.CharField(label='What is wrong?', widget=forms.Textarea(attrs={'rows': 3}))
    unsafe = forms.BooleanField(
        label='It is unsafe to use (take it out of use now)', required=False)


class InspectionForm(forms.Form):
    findings = forms.CharField(label='What did you find?', widget=forms.Textarea(attrs={'rows': 3}))
    repair_done = forms.CharField(label='Repair done (if any)', required=False,
                                  widget=forms.Textarea(attrs={'rows': 2}))
    repair_cost = forms.DecimalField(label='Repair cost (AED)', required=False, min_value=0,
                                     max_digits=10, decimal_places=2)
    condition = forms.ChoiceField(
        label='Condition after inspection', required=False,
        choices=[('', 'Unchanged')] + list(Asset.Condition.choices))
    outcome = forms.ChoiceField(label='Result', choices=MaintenanceTask.Outcome.choices)


class ProposalForm(forms.Form):
    action = forms.ChoiceField(label='Proposed action', choices=EndOfLifeProposal.Action.choices)
    reason = forms.CharField(label='Why, and why it cannot be reused or repaired',
                             widget=forms.Textarea(attrs={'rows': 3}))
