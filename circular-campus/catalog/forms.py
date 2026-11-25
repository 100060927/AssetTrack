from django import forms

from accounts.models import Department

from .models import Asset, Category


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = ['name', 'description', 'category', 'condition', 'tags', 'location',
                  'quantity', 'replacement_unit_cost', 'unit_mass_kg']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].empty_label = 'Choose a category'

    def clean_quantity(self):
        quantity = self.cleaned_data['quantity']
        if quantity < 1:
            raise forms.ValidationError('Quantity must be at least 1.')
        return quantity


class AssetEditForm(AssetForm):
    """UC-15: the same fields as registration, plus a reason for the record."""
    reason = forms.CharField(label='Reason for the change', required=False,
                             help_text='Required when you change the quantity (for example, a recount).')


class AllAssetsFilter(forms.Form):
    q = forms.CharField(label='Keyword', required=False)
    department = forms.ModelChoiceField(
        Department.objects.all(), required=False, empty_label='All departments')
    category = forms.ModelChoiceField(Category.objects.all(), required=False, empty_label='Any category')
    state = forms.ChoiceField(label='Status', required=False,
                              choices=[('', 'Any status')] + list(Asset.State.choices))
    condition = forms.ChoiceField(
        choices=[('', 'Any condition')] + list(Asset.Condition.choices), required=False)


class SearchForm(forms.Form):
    q = forms.CharField(label='Keyword', required=False)
    category = forms.ModelChoiceField(Category.objects.all(), required=False, empty_label='Any category')
    condition = forms.ChoiceField(
        choices=[('', 'Any condition')] + list(Asset.Condition.choices), required=False)
    department = forms.ModelChoiceField(
        Department.objects.filter(active=True), required=False, empty_label='Any department')
