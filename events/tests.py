import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Guest
from .views import generate_dynamic_token


class CheckInTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('staff', password='safe-password')
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.guest = Guest.objects.create(name='Invitada')
        self.static_url = reverse('api_check_in')
        self.dynamic_url = reverse('api_check_in_dynamic', args=[
            self.guest.uuid, generate_dynamic_token(str(self.guest.uuid))
        ])

    def test_checkin_endpoints_require_a_session_even_with_valid_qr(self):
        self.assertEqual(self.client.post(self.static_url, {'uuid': self.guest.uuid}).status_code, 302)
        self.assertEqual(self.client.post(self.dynamic_url).status_code, 302)
        self.assertEqual(self.client.get(self.dynamic_url).status_code, 302)
        self.assertFalse(Guest.objects.get(pk=self.guest.pk).is_checked_in)

    def test_browser_api_requires_csrf_and_reports_duplicates(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(self.static_url, {'uuid': self.guest.uuid})
        self.assertEqual(response.status_code, 403)
        client.get(reverse('scanner'))
        csrf = client.cookies['csrftoken'].value
        first = client.post(self.static_url, {'uuid': self.guest.uuid}, HTTP_X_CSRFTOKEN=csrf)
        second = client.post(self.static_url, {'uuid': self.guest.uuid}, HTTP_X_CSRFTOKEN=csrf)
        self.assertFalse(first.json()['already_checked'])
        self.assertTrue(second.json()['already_checked'])
        self.assertIsNotNone(Guest.objects.get(pk=self.guest.pk).checked_in_at)

    def test_opening_dynamic_qr_only_previews_and_signed_form_confirms(self):
        self.client.force_login(self.user)
        preview = self.client.get(self.dynamic_url)
        self.assertContains(preview, 'Confirmar llegada')
        self.assertFalse(Guest.objects.get(pk=self.guest.pk).is_checked_in)
        confirmation = preview.context['confirmation_token']
        first = self.client.post(self.dynamic_url, {'confirmation_token': confirmation})
        self.assertFalse(first.context['already_checked'])
        second = self.client.post(self.dynamic_url, {'confirmation_token': confirmation})
        self.assertTrue(second.context['already_checked'])

    def test_signed_confirmation_is_bound_to_staff_and_guest(self):
        other = get_user_model().objects.create_user('other', password='safe-password')
        other.is_staff = True
        other.save(update_fields=['is_staff'])
        self.client.force_login(self.user)
        confirmation = self.client.get(self.dynamic_url).context['confirmation_token']
        self.client.force_login(other)
        invalid_url = reverse('api_check_in_dynamic', args=[self.guest.uuid, 'invalid'])
        result = self.client.post(invalid_url, {'confirmation_token': confirmation})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(Guest.objects.get(pk=self.guest.pk).is_checked_in)

    def test_authenticated_non_staff_cannot_register_an_arrival(self):
        viewer = get_user_model().objects.create_user('viewer', password='safe-password')
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(self.static_url, {'uuid': self.guest.uuid}).status_code, 403)
        self.assertEqual(self.client.post(self.dynamic_url).status_code, 403)
        self.assertEqual(self.client.post(reverse('mobile_checkin'),
                                          json.dumps({'uuid': str(self.guest.uuid)}),
                                          content_type='application/json').status_code, 403)
        self.assertFalse(Guest.objects.get(pk=self.guest.pk).is_checked_in)

    def test_group_post_requires_signed_confirmation(self):
        self.client.force_login(self.user)
        companion = Guest.objects.create(name='Acompañante', parent=self.guest, guest_type='companion')
        url = reverse('group_check_in', args=[self.guest.uuid, generate_dynamic_token(str(self.guest.uuid))])
        self.assertEqual(self.client.post(url, {'guest_id': companion.pk}).status_code, 200)
        self.assertFalse(Guest.objects.get(pk=companion.pk).is_checked_in)
        preview = self.client.get(url)
        token = preview.context['confirmation_token']
        response = self.client.post(url, {'guest_id': companion.pk, 'confirmation_token': token})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Guest.objects.get(pk=companion.pk).is_checked_in)

    def test_mobile_duplicate_response_uses_same_conditional_update(self):
        self.client.force_login(self.user)
        url = reverse('mobile_checkin')
        payload = json.dumps({'uuid': str(self.guest.uuid)})
        first = self.client.post(url, payload, content_type='application/json')
        second = self.client.post(url, payload, content_type='application/json')
        self.assertFalse(first.json()['already_checked'])
        self.assertTrue(second.json()['already_checked'])

    def test_registration_continues_after_first_hundred_raffle_numbers(self):
        Guest.objects.bulk_create([Guest(name=f'Invitado {n}', raffle_number=n)
                                   for n in range(1, 101) if n != self.guest.raffle_number])
        extra = Guest.objects.create(name='Invitada 101')
        self.assertEqual(extra.raffle_number, 101)
