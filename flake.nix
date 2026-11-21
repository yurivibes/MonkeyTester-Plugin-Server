{
  description = "MonkeyTester-Plugin-Server: MonkeyTester device plugin for the ESP32 server (hub) boards + the fake software server";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };
        # zeroconf: the fake server's mDNS advertisement (_tsserver._tcp)
        python = pkgs.python3.withPackages (ps: [ ps.pyserial ps.zeroconf ]);
        src = pkgs.lib.cleanSource ./.;

        plugin = pkgs.writeShellApplication {
          name = "monkeytester-esp32-server";
          runtimeInputs = [ python pkgs.esptool ];
          text = ''
            export PLUGIN_PORT="''${PLUGIN_PORT:-8093}"
            exec python3 ${src}/esp32_plugin.py "$@"
          '';
        };

        tests = pkgs.writeShellApplication {
          name = "monkeytester-esp32-server-tests";
          runtimeInputs = [ python ];
          text = ''
            cd ${src}
            exec python3 -m unittest discover -s tests -v "$@"
          '';
        };
      in {
        packages.default = plugin;
        packages.monkeytester-esp32-server = plugin;

        # nix run .#         -> esp32-server plugin on :8093 (fake server :8092)
        # nix run .#tests    -> unit tests against fake boards / fake server
        apps.default = { type = "app"; program = "${plugin}/bin/monkeytester-esp32-server"; };
        apps.tests = { type = "app"; program = "${tests}/bin/monkeytester-esp32-server-tests"; };

        devShells.default = pkgs.mkShell { packages = [ python pkgs.esptool ]; };
      });
}
