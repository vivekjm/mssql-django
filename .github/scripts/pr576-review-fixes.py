"""Prepare PR 576 candidates in an isolated checkout; never push a PR branch."""
from pathlib import Path
import sys


def replace_once(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1, (str(path), old[:100], text.count(old))
    path.write_text(text.replace(old, new))


TESTS = '''
    @skipUnlessDBFeature('supports_json_openjson')
    def test_json_null_keys_preserve_trailing_spaces(self):
        for key in ('key', 'caf\u00e9', '\U0001f4a1', "o'reilly"):
            for nested in (False, True):
                with self.subTest(key=key, nested=nested):
                    values = [
                        {key: None, key + ' ': 1},
                        {key: 1, key + ' ': None},
                        {key + ' ': None},
                        {},
                    ]
                    rows = [JSONModel.objects.create(
                        value={'nested': value} if nested else value
                    ) for value in values]
                    queryset = JSONModel.objects.filter(pk__in=[row.pk for row in rows])
                    prefix = 'value__nested__' if nested else 'value__'
                    lookup = {prefix + key: None}
                    spaced_lookup = {prefix + key + ' ': None}
                    self.assertSequenceEqual(queryset.filter(**lookup), [rows[0]])
                    self.assertSequenceEqual(queryset.exclude(**lookup), [rows[1]])
                    self.assertSequenceEqual(
                        queryset.filter(**spaced_lookup).order_by('pk'), rows[1:3]
                    )
                    self.assertSequenceEqual(queryset.exclude(**spaced_lookup), [rows[0]])

    @skipUnlessDBFeature('supports_json_openjson')
    def test_json_null_keys_in_filtered_aggregates(self):
        cases = [
            ('value__key', [{'key': None}, {'key': 1}, {}]),
            ('value__nested__key', [
                {'nested': {'key': None}}, {'nested': {'key': 1}}, {'nested': {}}
            ]),
            ('value__0', [[None], [1], []]),
            ('value__items__0', [{'items': [None]}, {'items': [1]}, {'items': []}]),
        ]
        for lookup_name, values in cases:
            with self.subTest(lookup=lookup_name):
                rows = [JSONModel.objects.create(value=value) for value in values]
                queryset = JSONModel.objects.filter(pk__in=[row.pk for row in rows])
                condition = Q(**{lookup_name: None})
                for source in (queryset, queryset.distinct(), queryset.order_by('pk')[:3]):
                    self.assertEqual(source.aggregate(
                        null_count=Count('pk', filter=condition),
                        non_null_count=Count('pk', filter=~condition),
                    ), {'null_count': 1, 'non_null_count': 1})
                self.assertEqual(queryset.annotate(
                    payload=Value({'key': None}, output_field=JSONField())
                ).aggregate(n=Count('pk', filter=Q(payload__key=None))), {'n': 3})

    @skipUnlessDBFeature('supports_json_openjson')
    def test_json_null_keys_in_grouped_case_annotations(self):
        cases = [
            ('value__key', [{'key': None}, {'key': 1}, {}]),
            ('value__nested__key', [
                {'nested': {'key': None}}, {'nested': {'key': 1}}, {'nested': {}}
            ]),
            ('value__0', [[None], [1], []]),
            ('value__items__0', [{'items': [None]}, {'items': [1]}, {'items': []}]),
        ]
        for lookup_name, values in cases:
            with self.subTest(lookup=lookup_name):
                rows = [JSONModel.objects.create(value=value) for value in values]
                queryset = JSONModel.objects.filter(pk__in=[row.pk for row in rows])
                grouped = queryset.annotate(flag=Case(
                    When(Q(**{lookup_name: None}), then=Value(1)),
                    default=Value(0), output_field=IntegerField(),
                )).values('flag').annotate(n=Count('pk')).order_by('flag')
                compiler = grouped.query.get_compiler(using='default')
                self.assertEqual(compiler.as_sql(), compiler.as_sql())
                self.assertSequenceEqual(
                    grouped, [{'flag': 0, 'n': 2}, {'flag': 1, 'n': 1}]
                )

'''

COMPILER_HELPERS = '''    def _compile_json_null_predicate(self, sql, params):
        # SELECT/GROUP BY and aggregate expressions cannot contain scalar
        # subqueries on SQL Server. Evaluate the nullable indicator in FROM.
        applies = getattr(self, '_json_null_applies', None)
        if applies is None:
            # UPDATE/DELETE and callers compiling expressions on their own do
            # not have a SELECT FROM clause to attach an APPLY to.
            return '(%s) = 1' % sql, params
        params = tuple(params)
        for alias, existing_sql, existing_params in applies:
            if sql == existing_sql and params == existing_params:
                return '%s.[value] = 1' % self.connection.ops.quote_name(alias), ()
        used_aliases = {
            alias.lower() for alias in (
                *self.query.alias_map, *getattr(self.query, 'external_aliases', {}),
                *(item[0] for item in applies), 'subquery',
            )
        }
        index = len(applies)
        alias = '__mssql_json_null_%d' % index
        while alias.lower() in used_aliases:
            index += 1
            alias = '__mssql_json_null_%d' % index
        applies.append((alias, sql, params))
        return '%s.[value] = 1' % self.connection.ops.quote_name(alias), ()

    def _get_json_null_apply_clauses(self):
        clauses, params = [], []
        for alias, sql, sql_params in self._json_null_applies:
            clauses.append('OUTER APPLY (%s) AS %s' % (
                sql, self.connection.ops.quote_name(alias),
            ))
            params.extend(sql_params)
        return clauses, params

'''

AGGREGATE_COMPILER = '''class SQLAggregateCompiler(compiler.SQLAggregateCompiler, SQLCompiler):
    def as_sql(self):
        previous_applies = getattr(self, '_json_null_applies', None)
        self._json_null_applies = []
        try:
            sql, params = super().as_sql()
            applies, apply_params = self._get_json_null_apply_clauses()
            if applies:
                sql += ' ' + ' '.join(applies)
            return sql, tuple(params) + tuple(apply_params)
        finally:
            self._json_null_applies = previous_applies
'''


def add_tests():
    path = Path('testapp/tests/test_jsonfield.py')
    replace_once(path, 'from django.test import TestCase',
                 'from django.db.models import Case, Count, IntegerField, JSONField, Q, Value, When\n'
                 'from django.test import TestCase, skipUnlessDBFeature')
    marker = '    @skipUnless(VERSION >= (3, 1), "JSONField not supported in Django versions < 3.1")\n    def test_json_null_numeric_key_uses_array_index_semantics(self):'
    replace_once(path, marker, TESTS + marker)


def apply_fixes():
    path = Path('mssql/functions.py')
    replace_once(path,
        '            return (\n'
        '                "(SELECT MAX(CASE WHEN [item].[type] = 0 THEN 1 ELSE 0 END) "\n',
        '            return compiler._compile_json_null_predicate(\n'
        '                "SELECT MAX(CASE WHEN [item].[type] = 0 THEN 1 ELSE 0 END) AS [value] "\n')
    replace_once(path, '                "AND [item].[key] = %%s) = 1" % parent_json,',
                 '                "AND [item].[key] = %%s" % parent_json,')
    replace_once(path,
        '        return (\n'
        '            "(SELECT MAX(CASE WHEN [type] = 0 THEN 1 ELSE 0 END) "\n'
        '            "FROM %s WHERE [key] = %%s) = 1" % openjson,\n'
        '            tuple(lhs_params) + (final_key,),\n'
        '        )',
        '        # SQL Server pads strings for equality, even under BIN2. Compare\n'
        '        # the UTF-16 byte lengths as well so trailing spaces stay significant.\n'
        '        return compiler._compile_json_null_predicate(\n'
        '            "SELECT MAX(CASE WHEN [type] = 0 THEN 1 ELSE 0 END) AS [value] "\n'
        '            "FROM %s WHERE [key] = %%s "\n'
        '            "AND DATALENGTH([key]) = DATALENGTH(CAST(%%s AS nvarchar(max)))" % openjson,\n'
        '            tuple(lhs_params) + (final_key, final_key),\n'
        '        )')
    path = Path('mssql/compiler.py')
    replace_once(path, 'class SQLCompiler(compiler.SQLCompiler):\n\n',
                 'class SQLCompiler(compiler.SQLCompiler):\n\n' + COMPILER_HELPERS)
    replace_once(path,
        '        refcounts_before = self.query.alias_refcount.copy()\n        try:\n',
        '        refcounts_before = self.query.alias_refcount.copy()\n'
        "        previous_applies = getattr(self, '_json_null_applies', None)\n"
        '        self._json_null_applies = []\n        try:\n')
    replace_once(path,
        "                result += [', '.join(out_cols)]\n                if from_:\n",
        "                applies, apply_params = self._get_json_null_apply_clauses()\n"
        '                from_.extend(applies)\n'
        '                f_params.extend(apply_params)\n'
        "                result += [', '.join(out_cols)]\n                if from_:\n")
    replace_once(path,
        '            self.query.reset_refcounts(refcounts_before)\n\n    def compile(',
        '            self.query.reset_refcounts(refcounts_before)\n'
        '            self._json_null_applies = previous_applies\n\n    def compile(')
    replace_once(path, 'class SQLAggregateCompiler(compiler.SQLAggregateCompiler, SQLCompiler):\n    pass',
                 AGGREGATE_COMPILER.rstrip())


if __name__ == '__main__':
    {'tests': add_tests, 'fix': apply_fixes}[sys.argv[1]]()
