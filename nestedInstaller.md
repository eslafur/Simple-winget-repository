## Nested installers in the WinGet REST source

This document explains how **nested installers** are represented and processed in this repository, based on the OpenAPI spec and the C# models.

---

### Concept

- A **nested installer** is an installer that is **contained inside another installer file**, typically an archive (for example, a `.zip` that contains one or more portable binaries).
- In the WinGet REST data model, a *single* `Installer` record can describe:
  - The **outer container** (e.g., `InstallerType: "zip"` with an `InstallerUrl` that points to the archive).
  - The **inner installers** via:
    - `NestedInstallerType`: what kind of installers the archive contains (e.g., `portable`, `msi`, etc.).
    - `NestedInstallerFiles`: a list of specific files inside the archive, with optional aliases for portable packages.
- The WinGet client uses this information to:
  - Know **how to treat** the archive (for example, as a portable installer container),
  - Find the correct **entry points** (for example, which binary inside a `.zip` to add to PATH, and with what command alias).

---

### Schema-level view

In the OpenAPI definition (`documentation/WinGet-1.10.0.yaml`), nested installers are described under the `Installer` schema:

- `NestedInstallerType`:
  - Defined as an enum of supported nested installer types:
    - `msix`, `msi`, `appx`, `exe`, `inno`, `nullsoft`, `wix`, `burn`, `portable`
  - Used when `InstallerType` is something like `"zip"`:
    - `InstallerType: "zip"`
    - `NestedInstallerType: "portable"` (for example)
- `NestedInstallerFiles`:
  - An array of `NestedInstallerFile` objects:
    - `RelativeFilePath` (required)
    - `PortableCommandAlias` (optional; applies only when `NestedInstallerType` is a portable kind)

From the spec:

- `NestedInstallerFile` (simplified):
  - `RelativeFilePath`: string, required  
    Path to the nested installer file **inside** the archive.
  - `PortableCommandAlias`: string, optional  
    The command alias WinGet should use for invoking the nested portable package.
- `NestedInstallerFiles`:
  - Array of `NestedInstallerFile`
  - `nullable: true`
  - `uniqueItems: true`
  - `maxItems: 1024`

---

### C# data model in this repo

#### Installer-level properties

In `Installer` (`src/WinGet.RestSource.Utils/Models/Schemas/Installer.cs`), nested installers show up as:

- `public string NestedInstallerType { get; set; }`
  - Validated by `NestedInstallerTypeValidator`.
  - Mirrors the `NestedInstallerType` enum in the OpenAPI spec.
- `public NestedInstallerFiles NestedInstallerFiles { get; set; }`
  - `NestedInstallerFiles` is a custom array wrapper around `NestedInstallerFile` objects.
  - These are validated as a whole by `ApiDataValidator` when present.

The `Validate` method of `Installer` ensures:

- Core Installer requirements (e.g., `InstallerUrl`/`InstallerSha256` vs. `MSStoreProductIdentifier` based on `InstallerType`).
- If `NestedInstallerFiles` is not null, it runs the nested validation on each `NestedInstallerFile`.

#### NestedInstallerFile and NestedInstallerFiles

In `NestedInstallerFile` (`src/WinGet.RestSource.Utils/Models/Objects/NestedInstallerFile.cs`):

- `RelativeFilePath`:
  - Required.
  - Validated by `NestedInstallerFileRelativeFilePathValidator` (enforces non-empty, reasonable length/path).
  - Represents the **path within the outer archive** (e.g., `"bin/mytool.exe"`).
- `PortableCommandAlias`:
  - Optional.
  - Validated by `PortableCommandAliasValidator` (length and token rules).
  - Used only when the nested installer is a **portable** package:
    - This alias becomes the command name WinGet exposes to the user (for example, `mytool`).

In `NestedInstallerFiles` (`src/WinGet.RestSource.Utils/Models/Arrays/NestedInstallerFiles.cs`):

- Inherits from `ApiArray<Objects.NestedInstallerFile>`.
- Configured with:
  - `AllowNull = true`
  - `UniqueItems = true`
  - `MaxItems = 1024`

So `NestedInstallerFiles` is simply the strongly-typed representation of the `NestedInstallerFiles` array defined in the OpenAPI spec.

---

### How nested installers are populated when ingesting manifests

The helper `PackageManifestUtils` (`src/WinGet.RestSource.Utils/Utils/PackageManifestUtils.cs`) converts incoming YAML/JSON manifests into the REST source internal models.

For installers, the relevant logic is:

- When building `newInstaller` from a manifest installer:
  - `newInstaller.NestedInstallerType = installer.NestedInstallerType ?? manifest.NestedInstallerType;`
    - If the installer entry itself has a `NestedInstallerType`, that value is used.
    - Otherwise, it falls back to a package/manifest-level `NestedInstallerType` if defined.
  - `newInstaller.NestedInstallerFiles = AddNestedInstallerFiles(installer.NestedInstallerFiles) ?? AddNestedInstallerFiles(manifest.NestedInstallerFiles);`
    - `AddNestedInstallerFiles(...)` converts the manifest’s nested-file descriptions into `NestedInstallerFile` objects.
    - It chooses installer-level nested files first, then falls back to manifest-level nested files.

The `AddNestedInstallerFiles` helper:

- Takes a list of manifest-side nested files (`InstallerNestedInstallerFile`).
- For each entry, creates a `NestedInstallerFile`:
  - Copies `RelativeFilePath`.
  - Copies `PortableCommandAlias`.
- Adds them to a `NestedInstallerFiles` collection.

This means:

- Nested installer metadata can be specified either at:
  - The **installer level** (applies to that specific installer row), or
  - The **manifest version level** (shared as defaults).
- For a given installer record, the **installer-level** values take precedence over manifest-level defaults.

---

### Practical behavior summary

When you define an installer that uses nested installers (for example, an archive containing one or more portable executables):

1. **Outer installer fields**:
   - `InstallerType` should represent the outer package type (e.g., `"zip"`).
   - `InstallerUrl` and `InstallerSha256` must still describe the actual file that the client downloads (the zip file).
2. **Nested installer metadata**:
   - Set `NestedInstallerType` to the type of installers inside the archive (e.g., `"portable"`).
   - Provide `NestedInstallerFiles`:
     - Each entry must have `RelativeFilePath` to the inner file.
     - For portable packages, optionally set `PortableCommandAlias` to expose a command name.
3. **Client interpretation**:
   - WinGet downloads the outer installer file.
   - Based on `InstallerType` + `NestedInstallerType`, the client knows it must:
     - Treat the outer file as an archive,
     - Locate each `RelativeFilePath` inside that archive,
     - For portable installers:
       - Extract/copy the file to an appropriate location,
       - Add shims or PATH entries using `PortableCommandAlias` where provided.
4. **Validation and storage**:
   - The REST source validates paths and aliases using the dedicated validators.
   - The nested metadata is stored as part of the `Installer` document and returned verbatim to the client in `/packageManifests` responses.

---

### When to use nested installers in your own implementation

Use nested installers when:

- Your **actual distribution artifact** is an archive (`zip`, etc.) that contains one or more executables that should appear as **separate commands** (portable apps).
- You want WinGet to understand **which file inside the archive** is the “real” installer or entry point, and (for portable) what **command name** to expose.

Do **not** use nested installers when:

- The installer file (`msi`, `exe`, `msix`) is directly executable and does not require archive unpacking to locate the true payload.


