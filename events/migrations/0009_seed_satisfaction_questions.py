from django.db import migrations


DEFAULT_SURVEY_QUESTIONS = [
    ("¿Cómo calificarías la organización general del evento?", "rating"),
    ("¿Cómo calificarías la atención recibida durante el registro y acceso?", "rating"),
    ("¿El servicio cumplió con tus expectativas?", "yes_no"),
    ("¿Qué mejorarías para el próximo año?", "text"),
    ("¿Recomendarías este evento o servicio?", "yes_no"),
]


def seed_questions(apps, schema_editor):
    questions = apps.get_model('events', 'SatisfactionQuestion').objects.using(schema_editor.connection.alias)
    if questions.exists():
        return
    for order, (text, question_type) in enumerate(DEFAULT_SURVEY_QUESTIONS, start=1):
        questions.create(text=text, question_type=question_type, order=order)


class Migration(migrations.Migration):
    dependencies = [('events', '0008_satisfactionquestion_guest_amount_paid_guest_gender_and_more')]
    operations = [migrations.RunPython(seed_questions, migrations.RunPython.noop)]
