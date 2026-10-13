from django.db import migrations


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute("CREATE INDEX knowledge_chunk_fts ON api_knowledgechunk USING gin (to_tsvector('russian'::regconfig, COALESCE(text, '')))")
    schema_editor.execute("""CREATE INDEX knowledge_card_fts ON api_knowledgecard USING gin
        (to_tsvector('russian'::regconfig, COALESCE(title, '') || ' ' || COALESCE(problem, '') || ' ' ||
        COALESCE(causes, '') || ' ' || COALESCE(solution, '') || ' ' || COALESCE(restrictions, '')))
        WHERE status = 'approved'""")


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP INDEX IF EXISTS knowledge_chunk_fts')
        schema_editor.execute('DROP INDEX IF EXISTS knowledge_card_fts')


class Migration(migrations.Migration):
    dependencies = [('api', '0058_knowledgedocument_knowledgechunk')]
    operations = [migrations.RunPython(create_indexes, drop_indexes)]
