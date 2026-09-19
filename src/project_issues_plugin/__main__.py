import sys

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from project_issues_plugin.cli import main as cli_main

        sys.exit(cli_main(sys.argv[1:]))
    from project_issues_plugin.server import main

    main()
