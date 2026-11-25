from django import forms

from .models import ResourceRequest


class RequestForm(forms.ModelForm):
    class Meta:
        model = ResourceRequest
        fields = ['title', 'purpose', 'category', 'quantity', 'min_condition', 'required_date']
        widgets = {
            'purpose': forms.Textarea(attrs={'rows': 3}),
            'required_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].empty_label = 'Any category'

    def clean_quantity(self):
        quantity = self.cleaned_data['quantity']
        if quantity < 1:
            raise forms.ValidationError('Quantity must be at least 1.')
        return quantity
