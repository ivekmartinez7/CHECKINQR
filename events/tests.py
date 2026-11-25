import json
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from PIL import Image
from django.urls import reverse

from .models import ChatMessage, Guest, SatisfactionQuestion, SatisfactionResponse
from .tickets import whatsapp_phone
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


@override_settings(PUBLIC_BASE_URL='https://invitaciones.example.org')
class ReleaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser('admin-test', password='unrelated-test-passphrase')
        cls.staff = User.objects.create_user('staff-test', password='test-passphrase', is_staff=True)
        cls.viewer = User.objects.create_user('viewer-test')
        cls.guest = Guest.objects.create(name='Belén & José', phone='+52 (228) 123-4567', amount_paid=321.04)

    def test_health_robots_and_private_dashboard(self):
        self.assertEqual(self.client.get(reverse('healthz')).json(), {'status': 'ok'})
        self.assertContains(self.client.get(reverse('robots_txt')), 'Disallow: /api/')
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 302)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 403)

    def test_whatsapp_phone_normalization(self):
        for phone in ('228 123 4567', '+52 (228) 123-4567', '+52 1 228-123-4567'):
            self.assertEqual(whatsapp_phone(phone), '5212281234567')
        self.assertEqual(whatsapp_phone('+1 (212) 555-0100'), '12125550100')
        self.assertEqual(whatsapp_phone('https://example.org'), '')

    def test_dashboard_shares_group_ticket_using_public_origin(self):
        self.guest.passes = 2
        self.guest.save(update_fields=['passes'])
        Guest.objects.create(name='Acompañante', parent=self.guest, guest_type='companion', phone=self.guest.phone)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard'))
        for guest in response.context['guests']:
            link = urlsplit(guest.whatsapp_link)
            self.assertEqual(link.hostname, 'wa.me')
            self.assertEqual(link.path, '/5212281234567')
            message = parse_qs(link.query)['text'][0]
            self.assertIn('Belén & José', message)
            self.assertIn('Pases del grupo: 2.', message)
            self.assertIn(f'https://invitaciones.example.org/ticket/{self.guest.uuid}/', message)

    def test_wa_preview_is_generic_and_never_marks_attendance(self):
        response = self.client.get(reverse('guest_ticket', args=[self.guest.uuid]), HTTP_USER_AGENT='WhatsApp')
        self.assertContains(response, 'Boleto digital · CheckInQR')
        self.assertNotContains(response, '321.04')
        self.assertIn('noindex', response.headers['X-Robots-Tag'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.is_checked_in)

    def test_dynamic_qr_is_uncached_square_and_rotates(self):
        url = reverse('qr_code_image', args=[self.guest.uuid])
        with patch('events.views.time.time', return_value=150):
            first = self.client.get(url)
        with patch('events.views.time.time', return_value=165):
            second = self.client.get(url)
        self.assertEqual(first.headers['Content-Type'], 'image/png')
        self.assertIn('no-store', first.headers['Cache-Control'])
        self.assertNotEqual(first.content, second.content)
        image = Image.open(BytesIO(first.content))
        self.assertGreaterEqual(image.width, 240)
        self.assertEqual(image.height, image.width)

    def test_draft_methods_do_not_change_registration(self):
        self.client.force_login(self.admin)
        for name in ('delete_guest', 'reset_checkin', 'resend_email'):
            self.assertEqual(self.client.get(reverse(name, args=[self.guest.pk])).status_code, 405)
        self.assertTrue(Guest.objects.filter(pk=self.guest.pk).exists())

    def test_staff_chat_protects_writes_and_admin_history(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        url = reverse('api_chat_messages')
        self.assertEqual(client.post(url, {'text': '¡Hola!'}).status_code, 403)
        client.get(reverse('scanner'))
        token = client.cookies['csrftoken'].value
        self.assertTrue(client.post(url, {'text': '¡Hola!'}, HTTP_X_CSRFTOKEN=token).json()['success'])
        self.assertEqual(client.delete(url, HTTP_X_CSRFTOKEN=token).status_code, 403)
        self.assertEqual(ChatMessage.objects.count(), 1)

    def test_native_json_and_browser_csrf_are_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        url = reverse('mobile_guest_reset', args=[self.guest.pk])
        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url).status_code, 415)
        self.assertEqual(client.post(url, '{}', content_type='application/json',
                                     HTTP_ORIGIN='http://testserver').status_code, 403)
        self.assertEqual(client.post(url, '{}', content_type='application/json').status_code, 200)

    def test_invalid_passes_do_not_create_partial_groups(self):
        self.client.force_login(self.admin)
        for passes in ('inválido', '0', '51'):
            response = self.client.post(reverse('dashboard'), {'name': 'Nuevo', 'passes': passes})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(Guest.objects.count(), 1)
        self.assertEqual(self.client.post(reverse('dashboard'), {'name': 'Grupo', 'passes': '2'}).status_code, 302)
        self.assertEqual(Guest.objects.get(name='Grupo').companions.count(), 1)

    def test_invalid_survey_answers_do_not_save_partial_responses(self):
        question = SatisfactionQuestion.objects.filter(question_type='rating').first()
        url = reverse('survey_view', args=[self.guest.uuid])
        self.assertEqual(self.client.post(url, {f'question_{question.pk}': 'fuera de rango'}).status_code, 400)
        self.assertEqual(SatisfactionResponse.objects.count(), 0)

    def test_failed_login_attempts_are_bounded(self):
        cache.clear()
        url = reverse('login')
        for _ in range(12):
            self.assertEqual(self.client.post(url, {'username': 'prueba-ausente', 'password': ''}).status_code, 200)
        self.assertEqual(self.client.post(url, {'username': 'prueba-ausente', 'password': ''}).status_code, 429)
        cache.clear()


class ExecutiveReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('report-admin')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_empty_report_and_json_are_read_only(self):
        SatisfactionQuestion.objects.all().delete()

        page = self.client.get(reverse('executive_report'))
        self.assertContains(page, 'Reporte Ejecutivo')
        self.assertEqual(page.context['summary']['total'], 0)
        data = self.client.get(reverse('executive_report_json')).json()
        self.assertEqual(data['summary']['total_paid'], 0)
        self.assertEqual(data['survey'], [])
        self.assertFalse(SatisfactionQuestion.objects.exists())

    def test_filtered_report_and_export_agree(self):
        Guest.objects.create(name='Invitada', gender='F', school='Escuela de prueba',
                             amount_paid='125.50', is_checked_in=True)
        Guest.objects.create(name='Invitado', gender='M', amount_paid='42.00')
        filters = {'gender': 'F'}

        page = self.client.get(reverse('executive_report'), filters)
        self.assertContains(page, 'Escuela de prueba')
        data = self.client.get(reverse('executive_report_json'), filters).json()
        self.assertEqual(data['filters'], filters)
        self.assertEqual(data['summary'], page.context['summary'])
        self.assertEqual(data['summary']['total'], 1)
        self.assertEqual(data['summary']['attendance_rate'], 100.0)
        self.assertEqual(data['summary']['total_paid'], 125.50)
