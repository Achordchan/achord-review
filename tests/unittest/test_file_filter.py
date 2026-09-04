from pr_agent.algo.file_filter import filter_ignored
from pr_agent.config_loader import global_settings


class TestIgnoreFilter:
    def test_no_ignores(self):
        """
        Test no files are ignored when no patterns are specified.
        """
        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        assert filter_ignored(files) == files, "Expected all files to be returned when no ignore patterns are given."

    def test_glob_ignores(self, monkeypatch):
        """
        Test files are ignored when glob patterns are specified.
        """
        monkeypatch.setattr(global_settings.ignore, 'glob', ['*.py'])

        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        expected = [
            files[1],
            files[2]
        ]

        filtered_files = filter_ignored(files)
        assert filtered_files == expected, f"Expected {[file.filename for file in expected]}, but got {[file.filename for file in filtered_files]}."

    def test_double_star_glob_matches_root_and_nested(self, monkeypatch):
        """A `**/`-prefixed glob must ignore its target at the repository root as
        well as nested: translate_globs_to_regexes adds a stripped root variant,
        so `**/package-lock.json` also excludes a root-level `package-lock.json`.
        """
        monkeypatch.setattr(global_settings.ignore, 'glob',
                            ['**/node_modules/**', '**/package-lock.json', '**/*.png'])
        names = [
            'package-lock.json',            # root lockfile
            'sub/package-lock.json',        # nested lockfile
            'node_modules/react/index.js',  # root node_modules
            'a/node_modules/x.js',          # nested node_modules
            'logo.png',                     # root asset
            'assets/logo.png',              # nested asset
            'src/main.ts',                  # kept
        ]
        files = [type('', (object,), {'filename': n})() for n in names]
        kept = [f.filename for f in filter_ignored(files)]
        assert kept == ['src/main.ts'], f"Expected only src/main.ts kept, got {kept}."

    def test_regex_ignores(self, monkeypatch):
        """
        Test files are ignored when regex patterns are specified.
        """
        monkeypatch.setattr(global_settings.ignore, 'regex', ['^file[2-4]\..*$'])

        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        expected = [
            files[0],
            files[4]
        ]

        filtered_files = filter_ignored(files)
        assert filtered_files == expected, f"Expected {[file.filename for file in expected]}, but got {[file.filename for file in filtered_files]}."

    def test_invalid_regex(self, monkeypatch):
        """
        Test invalid patterns are quietly ignored.
        """
        monkeypatch.setattr(global_settings.ignore, 'regex', ['(((||', '^file[2-4]\..*$'])

        files = [
            type('', (object,), {'filename': 'file1.py'})(),
            type('', (object,), {'filename': 'file2.java'})(),
            type('', (object,), {'filename': 'file3.cpp'})(),
            type('', (object,), {'filename': 'file4.py'})(),
            type('', (object,), {'filename': 'file5.py'})()
        ]
        expected = [
            files[0],
            files[4]
        ]

        filtered_files = filter_ignored(files)
        assert filtered_files == expected, f"Expected {[file.filename for file in expected]}, but got {[file.filename for file in filtered_files]}."
    
    def test_language_framework_ignores(self, monkeypatch):
        """
        Test files are ignored based on language/framework mapping (e.g., protobuf).
        """
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', ['protobuf', 'go_gen'])

        files = [
            type('', (object,), {'filename': 'main.go'})(),
            type('', (object,), {'filename': 'dir1/service.pb.go'})(),
            type('', (object,), {'filename': 'dir1/dir/data_pb2.py'})(),
            type('', (object,), {'filename': 'file.py'})(),
            type('', (object,), {'filename': 'dir2/file_gen.go'})(),
            type('', (object,), {'filename': 'file.generated.go'})()
        ]
        expected = [
            files[0],
            files[3]
        ]

        filtered = filter_ignored(files)
        assert filtered == expected, (
            f"Expected {[f.filename for f in expected]}, "
            f"but got {[f.filename for f in filtered]}"
        )

    def test_skip_invalid_ignore_language_framework(self, monkeypatch):
        """
        Test skipping of generated code filtering when ignore_language_framework is not a list
        """
        monkeypatch.setattr(global_settings.config, 'ignore_language_framework', 'protobuf')

        files = [
            type('', (object,), {'filename': 'main.go'})(),
            type('', (object,), {'filename': 'file.py'})(),
            type('', (object,), {'filename': 'dir1/service.pb.go'})(),
            type('', (object,), {'filename': 'file_pb2.py'})()
        ]
        expected = [
            files[0],
            files[1],
            files[2],
            files[3]
        ]

        filtered = filter_ignored(files)
        assert filtered == expected, (
            f"Expected {[f.filename for f in expected]}, "
            f"but got {[f.filename for f in filtered]}"
        )
