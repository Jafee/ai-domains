import contextlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import update


def snapshot(files):
    return {"root": update.ROOT_LIST, "snapshot_date": "2026-10-05", "files": files}


def archive(files, extra=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as output:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo("domain-list-community-commit/data/" + name)
            info.size = len(raw)
            output.addfile(info, io.BytesIO(raw))
        if extra is not None:
            output.addfile(extra)
    return stream.getvalue()


def rules(artifacts):
    return [line for line in artifacts["adguard.txt"].splitlines() if line and not line.startswith("!")]


def matches(rule, host):
    if rule.startswith("||"):
        domain = rule[2:-1]
        return host == domain or host.endswith("." + domain)
    return re.search(rule[1:-1], host) is not None


class ExpansionTests(unittest.TestCase):
    def test_recursive_includes_with_attribute_filters_and_deduplication(self):
        files = {
            update.ROOT_LIST: "include:provider @ai @-cn\ndomain:example.com\n",
            "provider": "include:nested\nfull:api.example.com @ai\n",
            "nested": "example.com @ai\nother.com @ai @cn\nunmarked.com\n",
        }
        output = update.build_artifacts(snapshot(files))
        self.assertEqual(rules(output), ["||api.example.com^", "||example.com^"])
        self.assertEqual(json.loads(output["metadata.json"])["source_file_count"], 3)

    def test_affiliations_support_virtual_categories_and_named_files(self):
        files = {
            update.ROOT_LIST: "include:virtual @ai\n",
            "provider": "example.com @ai &virtual\nignored.com &virtual\n",
        }
        output = update.build_artifacts(snapshot(files))
        self.assertEqual(rules(output), ["||example.com^"])
        self.assertIn("provider", json.loads(output["sources.json"])["files"])
        files["virtual"] = "explicit.com @ai\n"
        self.assertEqual(rules(update.build_artifacts(snapshot(files))), ["||example.com^", "||explicit.com^"])

    def test_missing_include_and_cycles_fail(self):
        for files in [
            {update.ROOT_LIST: "include:missing\n"},
            {update.ROOT_LIST: "include:loop\n", "loop": "include:" + update.ROOT_LIST},
        ]:
            with self.subTest(files=files), self.assertRaises(update.UpdateError):
                update.build_artifacts(snapshot(files))

    def test_invalid_names_annotations_and_rule_types_fail(self):
        for line in ["include:../../other", "include:valid &other", "example.com unexpected", "unknown:example.com", "example.com @-ai"]:
            with self.subTest(line=line), self.assertRaises(update.UpdateError):
                update.build_artifacts(snapshot({update.ROOT_LIST: line}))

    def test_comments_are_not_saved_as_source_data(self):
        output = update.build_artifacts(snapshot({update.ROOT_LIST: "# Upstream prose\nexample.com # inline prose\n"}))
        self.assertEqual(json.loads(output["sources.json"])["files"][update.ROOT_LIST], "example.com\n")

    def test_empty_output_is_rejected(self):
        with self.assertRaises(update.UpdateError):
            update.build_artifacts(snapshot({update.ROOT_LIST: "# no rules"}))


class ConversionTests(unittest.TestCase):
    def test_domain_and_full_entries_cover_all_subdomains_with_boundaries(self):
        for kind in ["domain", "full"]:
            rule = update.entry_rule(update.Entry(kind, "Api.Example.Com", frozenset(), "provider"))
            for host in ["api.example.com", "sub.api.example.com", "a.b.api.example.com"]:
                self.assertTrue(matches(rule, host))
            for host in ["example.com", "notapi.example.com", "api.example.com.evil.org"]:
                self.assertFalse(matches(rule, host))

    def test_invalid_domains_are_rejected(self):
        for value in ["", "-bad.com", "bad-.com", "bad..com", "https://example.com", "x" * 64 + ".com"]:
            with self.subTest(value=value), self.assertRaises(update.UpdateError):
                update.domain_rule(value)

    def test_regex_alternation_anchors_and_nested_subdomains(self):
        rule = update.regex_rule(r"^first\.example\.com$|^second\.example\.com$")
        for host in ["first.example.com", "a.b.first.example.com", "second.example.com", "a.second.example.com"]:
            self.assertTrue(matches(rule, host))
        for host in ["notfirst.example.com", "first.example.com.evil.org", "example.com"]:
            self.assertFalse(matches(rule, host))

    def test_regex_preserves_class_negation_and_escaped_carets(self):
        self.assertEqual(update.regex_rule(r"^[^^]+\.example\.com$"), r"/(?:^|\.)[^^]+\.example\.com$/")
        self.assertEqual(update.regex_rule(r"^foo\^bar\.com$"), r"/(?:^|\.)foo\^bar\.com$/")
        self.assertEqual(update.regex_rule(r"\Aexample\.com$"), r"/(?:^|\.)example\.com$/")

    def test_openai_dynamic_hosts_and_their_subdomains(self):
        rule = update.regex_rule(r"^chatgpt-async-webps-prod-\S+-\d+\.webpubsub\.azure\.com$")
        host = "chatgpt-async-webps-prod-eastus-123.webpubsub.azure.com"
        for value in [host, "a." + host, "a.b." + host]:
            self.assertTrue(matches(rule, value))
        for value in [host + ".evil.org", "chatgpt-async-webps-prod-eastus-abc.webpubsub.azure.com", "webpubsub.azure.com"]:
            self.assertFalse(matches(rule, value))

    def test_invalid_or_unsupported_regex_fails(self):
        for value in ["(", "^example/com$", "^[[:alpha:]]+$"]:
            with self.subTest(value=value), self.assertRaises(update.UpdateError):
                update.regex_rule(value)

    def test_keyword_escapes_dots_and_hyphens(self):
        entry = update.Entry("keyword", "ai-service.com", frozenset(), "provider")
        rule = update.entry_rule(entry)
        self.assertTrue(matches(rule, "sub.ai-service.com"))
        self.assertFalse(matches(rule, "ai-servicexcom"))


class FetchAndOutputTests(unittest.TestCase):
    def test_download_is_pinned_to_the_resolved_commit(self):
        sha = "a" * 40
        commit = {"sha": sha, "commit": {"committer": {"date": "2026-10-05T00:00:00Z"}}}
        source_archive = archive({update.ROOT_LIST: "example.com\n"})
        with patch.object(update, "fetch_bytes", side_effect=[json.dumps(commit).encode(), source_archive]) as fetch:
            result = update.latest_snapshot()
        self.assertEqual(fetch.call_args_list[1].args[0], f"https://codeload.github.com/{update.UPSTREAM}/tar.gz/{sha}")
        self.assertEqual(result["upstream_commit"], sha)
        self.assertEqual(rules(update.build_artifacts(result)), ["||example.com^"])

    def test_unsafe_commit_ids_and_missing_root_are_rejected(self):
        data = {"sha": "../master", "commit": {"committer": {"date": "2026-10-05T00:00:00Z"}}}
        with patch.object(update, "fetch_bytes", return_value=json.dumps(data).encode()) as fetch:
            with self.assertRaises(update.UpdateError):
                update.latest_snapshot()
            self.assertEqual(fetch.call_count, 1)
        with self.assertRaises(update.UpdateError):
            update.archive_sources(archive({"unrelated": "example.com"}))

    def test_symlink_in_data_is_rejected_without_extraction(self):
        extra = tarfile.TarInfo("domain-list-community-commit/data/unsafe")
        extra.type = tarfile.SYMTYPE
        extra.linkname = "/etc/passwd"
        with self.assertRaises(update.UpdateError):
            update.archive_sources(archive({update.ROOT_LIST: "example.com"}, extra))

    def test_transient_failure_retries_without_external_credentials(self):
        response = contextlib.nullcontext(io.BytesIO(b"ok"))
        with patch.object(update, "urlopen", side_effect=[URLError("temporary"), response]) as request, patch.object(update.time, "sleep"):
            self.assertEqual(update.fetch_bytes("https://example.org/data"), b"ok")
            self.assertEqual(request.call_count, 2)
            self.assertNotIn("Authorization", request.call_args.args[0].headers)

    def test_permanent_http_failure_does_not_retry(self):
        error = HTTPError("https://example.org", 404, "Not Found", {}, None)
        with patch.object(update, "urlopen", side_effect=error) as request:
            with self.assertRaises(update.UpdateError):
                update.fetch_bytes("https://example.org/data")
            self.assertEqual(request.call_count, 1)

    def test_failed_update_keeps_all_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in update.OUTPUT_FILES:
                (output / name).write_text("previous version")
            bad = output / "bad.json"
            bad.write_text(json.dumps(snapshot({update.ROOT_LIST: "include:missing"})))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(update.main(["--snapshot", str(bad), "--output-dir", directory]), 1)
            for name in update.OUTPUT_FILES:
                self.assertEqual((output / name).read_text(), "previous version")
            with patch.object(update, "latest_snapshot", side_effect=update.UpdateError("download failed")), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(update.main(["--output-dir", directory]), 1)
            for name in update.OUTPUT_FILES:
                self.assertEqual((output / name).read_text(), "previous version")

    def test_generation_is_reproducible_and_check_detects_changes(self):
        original = update.build_artifacts(snapshot({update.ROOT_LIST: "example.com\n"}))
        regenerated = update.build_artifacts(json.loads(original["sources.json"]))
        self.assertEqual(original, regenerated)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            output = Path(directory)
            update.write_artifacts(output, original)
            arguments = ["--snapshot", str(output / "sources.json"), "--output-dir", directory, "--check"]
            self.assertEqual(update.main(arguments), 0)
            (output / "adguard.txt").write_text("corrupted")
            self.assertEqual(update.main(arguments), 1)
            self.assertEqual((output / "adguard.txt").read_text(), "corrupted")


class CurrentSnapshotTests(unittest.TestCase):
    def test_current_snapshot_has_complete_domain_and_regex_coverage(self):
        source = json.loads((update.REPO_ROOT / "sources.json").read_text())
        artifacts = update.build_artifacts(source)
        generated_rules = rules(artifacts)
        metadata = json.loads(artifacts["metadata.json"])
        entries, _ = update.expand_sources({name: update.canonical_source(text) for name, text in source["files"].items()})
        expected = {update.entry_rule(entry) for entry in entries}
        self.assertEqual(set(generated_rules), expected)
        self.assertEqual(len(generated_rules), len(set(generated_rules)))
        self.assertEqual(len(generated_rules), metadata["total_rule_count"])
        for entry in entries:
            if entry.kind in {"domain", "full"}:
                for host in [entry.value.lower(), "a." + entry.value.lower(), "a.b." + entry.value.lower()]:
                    self.assertTrue(any(matches(rule, host) for rule in generated_rules), host)
        # Avoid assumptions about which providers upstream will add or remove.
        for rule in generated_rules:
            if rule.startswith("||"):
                domain = rule[2:-1]
                for near_miss in ["not" + domain, domain + ".example.invalid"]:
                    self.assertFalse(matches(rule, near_miss), near_miss)


if __name__ == "__main__":
    unittest.main()
