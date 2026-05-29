# CPU_Baseline - FPGA EM Field Renderer

## Project Overview
This is the CPU-based baseline implementation of the Electromagnetic (EM) Field Renderer. It serves as a reference implementation for validating the FPGA-accelerated version and enables performance comparison between CPU and FPGA implementations.

## Objectives
- Provide a pure CPU-based EM field calculation and rendering implementation
- Enable performance benchmarking against FPGA implementation
- Validate correctness of algorithms before FPGA deployment
- Create a portable reference version for development and testing
- Establish baseline performance metrics for optimization targets

## Architecture
- **Computation Layer**: Pure CPU-based EM field algorithms
- **Rendering Layer**: Software-based or GPU-accelerated visualization
- **Display Output**: 640x480 resolution rendering
- **No Hardware Dependency**: Runs on standard computing platforms

## Key Features
- Functional equivalent to FPGA implementation
- Cross-platform compatibility (Windows, Linux, etc.)
- No FPGA driver or hardware requirements
- Performance profiling and benchmarking tools
- Identical output format to FPGA version for validation

## To-Do List

### Development
- [ ] Implement EM field calculation algorithms in C++
- [ ] Create field computation pipeline
- [ ] Implement 3D visualization engine (640x480 resolution)
- [ ] Develop data structures for field storage
- [ ] Implement parameter configuration system

### Optimization & Profiling
- [ ] Profile CPU usage and memory consumption
- [ ] Identify performance bottlenecks
- [ ] Optimize critical computation paths
- [ ] Implement multi-threading where applicable
- [ ] Benchmark against FPGA implementation

### Validation & Testing
- [ ] Create unit tests for computation modules
- [ ] Implement correctness validation tests
- [ ] Compare output with FPGA implementation
- [ ] Test edge cases and boundary conditions
- [ ] Validate numerical accuracy

### User Interface
- [ ] Implement parameter control interface
- [ ] Create visualization mode selector
- [ ] Add performance metrics display
- [ ] Implement data export functionality
- [ ] Create settings/configuration dialogs

### Documentation
- [ ] Document algorithm implementations
- [ ] Create API documentation
- [ ] Document performance characteristics
- [ ] Provide build and usage instructions
- [ ] Create performance comparison guide

### Benchmarking
- [ ] Establish baseline performance metrics
- [ ] Document CPU timing for operations
- [ ] Compare against FPGA results
- [ ] Measure memory overhead
- [ ] Create performance report template

## Build Instructions
```bash
# Create build directory
mkdir build
cd build

# Configure and build
cmake ..
make

# Run tests
make test
```

## Running the Application
```bash
# Start the CPU baseline renderer
./cpu-em-renderer
```

## System Requirements
- **Processor**: Multi-core CPU recommended
- **Display Resolution**: 640 x 480
- **Memory**: TBD (varies with field resolution)
- **Graphics**: OpenGL support for rendering
- **OS**: Windows, Linux, macOS

## Performance Baseline
This implementation should establish:
- Computation time per field update
- Memory requirements for different field sizes
- Rendering frame rates at 640x480
- Power consumption (when available)

## Usage
1. Launch the CPU baseline renderer
2. Configure field parameters (same as FPGA version)
3. Run field calculations
4. View real-time visualization
5. Compare results with FPGA implementation
6. Export performance metrics and data

## Comparison with FPGA
This baseline serves as the reference for:
- **Correctness Validation**: Output comparison
- **Performance Target**: Speedup measurement
- **Feature Parity**: Ensure feature completeness
- **Algorithm Verification**: Before hardware synthesis

## Notes
- Use this for development and validation before FPGA testing
- Maintain exact algorithm equivalence with FPGA for valid comparison
- Document any performance optimizations made
- Compare results with FPGA to catch discrepancies early
- Can be used as fallback if FPGA hardware unavailable

## Dependencies
- C++ compiler (C++17 or later)
- CMake (version TBD)
- OpenGL development libraries
- Standard math libraries
- Optional: Python for data analysis/visualization

## Troubleshooting
- If compilation fails, check C++ standard and compiler version
- If rendering is slow, verify GPU drivers and OpenGL support
- For performance issues, run with profiler to identify bottlenecks
- Check logs for numerical errors in calculations
