from django.db import migrations, models
from django.utils.text import slugify


def generate_unique_slugs(apps, schema_editor):
    Salon = apps.get_model("reservas", "Salon")

    used_slugs = set()

    for salon in Salon.objects.all().order_by("id"):
        base_slug = slugify(salon.name) or f"salon-{salon.id}"
        slug = base_slug
        counter = 2

        while slug in used_slugs or Salon.objects.filter(slug=slug).exclude(pk=salon.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1

        salon.slug = slug
        salon.save(update_fields=["slug"])
        used_slugs.add(slug)


class Migration(migrations.Migration):

    dependencies = [
        ("reservas", "0019_staffinvitation"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                DROP INDEX IF EXISTS reservas_salon_slug_c1634057_like CASCADE;
                DROP INDEX IF EXISTS reservas_salon_slug_key CASCADE;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),

        migrations.AddField(
            model_name="salon",
            name="slug",
            field=models.SlugField(
                max_length=140,
                blank=True,
                null=True,
                db_index=False,
                verbose_name="Slug público",
                help_text="URL pública del salón. Ejemplo: lux-salon",
            ),
        ),

        migrations.RunPython(generate_unique_slugs, migrations.RunPython.noop),

        migrations.AlterField(
            model_name="salon",
            name="slug",
            field=models.SlugField(
                max_length=140,
                unique=True,
                blank=True,
                verbose_name="Slug público",
                help_text="URL pública del salón. Ejemplo: lux-salon",
            ),
        ),
    ]